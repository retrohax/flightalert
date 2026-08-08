# FlightAlert Tracker
#
# Tracks an aircraft by registration number using the ADS-B Exchange API
# and optionally sends Discord notifications for takeoff and landing events.
#
# Before running, you need to:
#
# 1) Download the airports.csv file:
# wget https://ourairports.com/data/airports.csv
#
# 2) Set the RAPIDAPI_KEY environment variable:
# export RAPIDAPI_KEY="your_rapidapi_key"
#
# 3) Set the DISCORD_WEBHOOK_URL environment variable:
# export DISCORD_WEBHOOK_URL="your_discord_webhook_url"
#
# NOTE: Discord is optional; if you don't set the webhook URL,
# the script will still run and log events to stdout.
#
import csv
import io
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

AIRPORTS_CSV_LOCAL_PATH = "airports.csv"
AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}

RUNWAYS_CSV_LOCAL_PATH = "runways.csv"

# We only get 10,000 api requests per month so we limit the polling
# frequency where we can without losing transition detection accuracy.
POLL_SECONDS_OFFLINE = 900
POLL_SECONDS_ON_GROUND = 30
POLL_SECONDS_NEAR_AIRPORT = 20
POLL_SECONDS_ALTITUDE_A = 60
POLL_SECONDS_ALTITUDE_B = 300
POLL_SECONDS_ALTITUDE_C = 900
POLL_INTERVAL_INCREMENT = 60
POLL_THRESHOLD_ALTITUDE_AB = 10000
POLL_THRESHOLD_ALTITUDE_BC = 30000

# The "airborne" aircraft has been offline too long, reset it to "on_ground".
WAIT_FOR_RESET_ENROUTE = 24*3600
# The "airborne" aircraft was near an airport and has been offline too long,
# reset it to "on_ground". This will force a landing event.
WAIT_FOR_RESET_NEAR_AIRPORT = 300
# The aircraft was "on_ground" and has been offline too long, change polling interval.
WAIT_FOR_OFFLINE = 3600

# If an aircraft is within this distance and altitude of an airport,
# we consider it "near" that airport.
NEAR_AIRPORT_NM_THRESHOLD = 5.0
NEAR_AIRPORT_AGL_THRESHOLD = 2000

# RapidAPI
_rapidapi_key = None
_rapidapi_host = None
_rapidapi_base_url = None

# Discord
_discord_webhook_url = None
_COLOR_GREEN  = 3066993   # takeoff, airborne
_COLOR_BLUE   = 3447003   # landing

# List of airports and runways loaded from .csv files.
_airports = []
_runways = []

_aircraft_stall_speed = 100.0  # knots

# Useful when starting mid-flight to avoid emitting an airborne event.
_suppress_first_event = False


logging.basicConfig(
	level=logging.DEBUG,
	format="%(asctime)s %(levelname)s %(message)s",
	datefmt="%Y-%m-%d %H:%M:%S",
)


@dataclass
class FlightSnapshot:
	registration: str
	icao: str
	squawk: str | None
	emergency: str | None
	altitude: str | int | None
	groundspeed: int | None
	lat: float | None
	lon: float | None
	track: float | None
	airport_code: str | None = None
	airport_name: str | None = None
	airport_location: str | None = None
	altitude_agl: int | None = None
	runway_airport_ident: str | None = None
	runway_end_ident: str | None = None
	runway_nm: float | None = None
	runway_altitude_agl: int | None = None
	runway_eta_seconds: float | None = None


@dataclass
class PlaneState:
	last_registration: str = ""
	last_icao: str = ""
	last_altitude: str | int | None = None
	last_groundspeed: int | None = None
	last_lat: float | None = None
	last_lon: float | None = None
	last_airport_code: str | None = None
	last_airport_name: str | None = None
	last_airport_location: str | None = None
	last_altitude_agl: int | None = None
	last_runway_airport_ident: str | None = None
	last_runway_end_ident: str | None = None
	last_runway_nm: float | None = None
	last_runway_altitude_agl: int | None = None
	last_runway_eta_seconds: float | None = None
	last_status: str = "on_ground"
	last_poll_seconds: int = 0
	last_contact: datetime = field(default_factory=lambda: datetime.fromtimestamp(0, timezone.utc))


def build_tracking_url(icao):
	return f"https://globe.adsbexchange.com/?icao={quote(icao)}"


def format_altitude(altitude):
	if isinstance(altitude, str):
		return f"{altitude}" if altitude else "unknown"
	if isinstance(altitude, int):
		return f"{altitude}ft"
	return "unknown"


def format_groundspeed(groundspeed):
	return f"{groundspeed}kt" if groundspeed is not None else "unknown"


def format_seconds(seconds):
	if seconds is None or seconds < 0:
		return "unknown"
	minutes = seconds / 60.0
	return f"{minutes:0.1f}min"


def format_airport_label(code, location):
	if code is None:
		return None
	if code and location:
		return f"{code} ({location})"
	return code


def time_since(start_time):
	return (datetime.now(timezone.utc) - start_time).total_seconds()


def normalize_poll_interval(seconds):
	# Round up to the nearest multiple of 60 seconds.
	return seconds + (60 - seconds % 60) if seconds % 60 != 0 else seconds


def get_poll_interval(status, altitude_agl, is_near_airport, last_contact, last_poll_seconds):
	if status == 'on_ground':
		if time_since(last_contact) >= WAIT_FOR_OFFLINE:
			return POLL_SECONDS_OFFLINE
		return POLL_SECONDS_ON_GROUND
	if status == 'airborne':
		if is_near_airport:
			return POLL_SECONDS_NEAR_AIRPORT
		seconds = normalize_poll_interval(last_poll_seconds)
		next_poll_interval = seconds + POLL_INTERVAL_INCREMENT
		if time_since(last_contact) >= next_poll_interval:
			return min(next_poll_interval, POLL_SECONDS_OFFLINE)
		if altitude_agl is None:
			return POLL_SECONDS_ALTITUDE_A
		if altitude_agl > POLL_THRESHOLD_ALTITUDE_BC:
			return POLL_SECONDS_ALTITUDE_C
		if altitude_agl > POLL_THRESHOLD_ALTITUDE_AB:
			return POLL_SECONDS_ALTITUDE_B
		return POLL_SECONDS_ALTITUDE_A
	# Should never reach here.
	return POLL_SECONDS_OFFLINE


def load_airports(airports_csv_path):
	global _airports
	_airports = []

	try:
		with open(airports_csv_path, "r", encoding="utf-8") as f:
			csv_text = f.read()
	except FileNotFoundError:
		logging.warning("Local airports CSV not found")
		raise SystemExit(1)

	reader = csv.DictReader(io.StringIO(csv_text))

	for row in reader:
		if row.get("type", "unknown") not in AIRPORT_TYPES:
			continue

		ident_raw = row.get("ident")
		if not isinstance(ident_raw, str) or not ident_raw.strip():
			continue

		try:
			ident = ident_raw.strip().upper()
			elevation_ft = int(row.get("elevation_ft"))
			lat = float(row.get("latitude_deg"))
			lon = float(row.get("longitude_deg"))
		except (TypeError, ValueError):
			continue

		municipality = row.get("municipality") if isinstance(row.get("municipality"), str) else ""
		country = row.get("iso_country") if isinstance(row.get("iso_country"), str) else ""
		location = ", ".join(part for part in [municipality, country] if part) or "unknown location"

		_airports.append(
			{
				"ident": ident,
				"name": row.get("name"),
				"location": location,
				"lat": lat,
				"lon": lon,
				"elevation_ft": elevation_ft,
			}
		)

	logging.debug("Loaded %d airports for takeoff/landing detection", len(_airports))

	return


def load_runways(runways_csv_path):
	global _runways
	_runways = []

	try:
		with open(runways_csv_path, "r", encoding="utf-8") as f:
			csv_text = f.read()
	except FileNotFoundError:
		logging.warning("Local runways CSV not found: %s", runways_csv_path)
		raise SystemExit(1)

	reader = csv.DictReader(io.StringIO(csv_text))

	if len(_airports) == 0:
		logging.warning("No airports loaded; cannot load runways")
		return
	
	airport_idents = {airport["ident"] for airport in _airports}

	for row in reader:
		airport_ident_raw = row.get("airport_ident")
		le_ident_raw = row.get("le_ident")
		he_ident_raw = row.get("he_ident")

		if not isinstance(airport_ident_raw, str) or not airport_ident_raw.strip():
			continue
		if not isinstance(le_ident_raw, str) or not le_ident_raw.strip():
			continue
		if not isinstance(he_ident_raw, str) or not he_ident_raw.strip():
			continue

		try:
			airport_ident = airport_ident_raw.strip().upper()
			length_ft = int(row.get("length_ft"))
			width_ft = int(row.get("width_ft"))
			closed = int(row.get("closed"))
			le_ident = le_ident_raw.strip().upper()
			le_latitude_deg = float(row.get("le_latitude_deg"))
			le_longitude_deg = float(row.get("le_longitude_deg"))
			le_elevation_ft = int(row.get("le_elevation_ft"))
			le_heading_degt = float(row.get("le_heading_degT"))
			he_ident = he_ident_raw.strip().upper()
			he_latitude_deg = float(row.get("he_latitude_deg"))
			he_longitude_deg = float(row.get("he_longitude_deg"))
			he_elevation_ft = int(row.get("he_elevation_ft"))
			he_heading_degt = float(row.get("he_heading_degT"))
		except (TypeError, ValueError):
			continue

		if (
			not airport_ident
			or length_ft <= 0
			or width_ft <= 0
			or closed != 0
			or not le_ident
			or not he_ident
		):
			continue

		if airport_ident not in airport_idents:
			#logging.debug(
			#	"Not loading runway %s/%s; references unknown airport %s",
			#	le_ident,
			#	he_ident,
			#	airport_ident
			#)
			continue

		surface = str(row.get("surface", "")).strip().upper()

		try:
			row_id = int(row.get("id"))
		except (TypeError, ValueError):
			row_id = None

		try:
			airport_ref = int(row.get("airport_ref"))
		except (TypeError, ValueError):
			airport_ref = None

		try:
			lighted = int(row.get("lighted"))
		except (TypeError, ValueError):
			lighted = None

		try:
			le_displaced_threshold_ft = int(row.get("le_displaced_threshold_ft"))
		except (TypeError, ValueError):
			le_displaced_threshold_ft = None

		try:
			he_displaced_threshold_ft = int(row.get("he_displaced_threshold_ft"))
		except (TypeError, ValueError):
			he_displaced_threshold_ft = None

		_runways.append(
			{
				"id": row_id,
				"airport_ref": airport_ref,
				"airport_ident": airport_ident,
				"length_ft": length_ft,
				"width_ft": width_ft,
				"surface": surface,
				"lighted": lighted,
				"closed": closed,
				"le_ident": le_ident,
				"le_latitude_deg": le_latitude_deg,
				"le_longitude_deg": le_longitude_deg,
				"le_elevation_ft": le_elevation_ft,
				"le_heading_degT": le_heading_degt,
				"le_displaced_threshold_ft": le_displaced_threshold_ft,
				"he_ident": he_ident,
				"he_latitude_deg": he_latitude_deg,
				"he_longitude_deg": he_longitude_deg,
				"he_elevation_ft": he_elevation_ft,
				"he_heading_degT": he_heading_degt,
				"he_displaced_threshold_ft": he_displaced_threshold_ft,
			}
		)

	logging.debug("Loaded %d runways for takeoff/landing detection", len(_runways))
	return


def haversine_nm(lat1, lon1, lat2, lon2):
	# Great-circle distance in nautical miles.
	r = 3440.065
	phi1 = math.radians(lat1)
	phi2 = math.radians(lat2)
	dphi = math.radians(lat2 - lat1)
	dlambda = math.radians(lon2 - lon1)
	a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
	c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
	return r * c


def ang_diff_deg(a, b):
	# Smallest angular difference in degrees, normalized to [0, 180].
	diff = abs((a - b + 180.0) % 360.0 - 180.0)
	return diff


def is_on_extended_centerline(
	lat,
	lon,
	track,
	last_lat,
	last_lon,
	threshold_lat,
	threshold_lon,
	runway_heading_degt,
	xt_max_nm=0.5,
	track_tol_deg=20.0,
	min_closure_nm=0.02,
):
	# Returns (is_match, cross_track_nm, track_error_deg, closure_nm).
	if (
		lat is None
		or lon is None
		or track is None
		or last_lat is None
		or last_lon is None
		or threshold_lat is None
		or threshold_lon is None
		or runway_heading_degt is None
	):
		return False, None, None, None

	r_nm = 3440.065

	phi1 = math.radians(threshold_lat)
	lam1 = math.radians(threshold_lon)
	phi3 = math.radians(lat)
	lam3 = math.radians(lon)

	dlam13 = lam3 - lam1
	y13 = math.sin(dlam13) * math.cos(phi3)
	x13 = math.cos(phi1) * math.sin(phi3) - math.sin(phi1) * math.cos(phi3) * math.cos(dlam13)
	theta13 = math.atan2(y13, x13)
	theta12 = math.radians(runway_heading_degt)

	dist13_nm = haversine_nm(threshold_lat, threshold_lon, lat, lon)
	delta13 = dist13_nm / r_nm

	xtrack_sin = math.sin(delta13) * math.sin(theta13 - theta12)
	xtrack_sin = max(-1.0, min(1.0, xtrack_sin))
	cross_track_nm = abs(r_nm * math.asin(xtrack_sin))

	track_error_deg = ang_diff_deg(track, runway_heading_degt)

	last_dist_nm = haversine_nm(last_lat, last_lon, threshold_lat, threshold_lon)
	closure_nm = last_dist_nm - dist13_nm

	is_match = (
		cross_track_nm <= xt_max_nm
		and track_error_deg <= track_tol_deg
		and closure_nm >= min_closure_nm
	)

	return is_match, cross_track_nm, track_error_deg, closure_nm


def find_nearest_runway(lat, lon, altitude, track, last_lat, last_lon, groundspeed):
	closest = None
	closest_nm = None
	closest_eta_seconds = None
	closest_end_ident = None
	closest_altitude_agl = None
	closest_cross_track_nm = None
	closest_track_error_deg = None
	closest_closure_nm = None

	for runway in _runways:
		for threshold_lat, threshold_lon, runway_heading_degt in (
			(runway["le_latitude_deg"], runway["le_longitude_deg"], runway["le_heading_degT"]),
			(runway["he_latitude_deg"], runway["he_longitude_deg"], runway["he_heading_degT"]),
		):
			end_ident = runway["le_ident"] if threshold_lat == runway["le_latitude_deg"] and threshold_lon == runway["le_longitude_deg"] else runway["he_ident"]
			is_match, cross_track_nm, track_error_deg, closure_nm = is_on_extended_centerline(
				lat=lat,
				lon=lon,
				track=track,
				last_lat=last_lat,
				last_lon=last_lon,
				threshold_lat=threshold_lat,
				threshold_lon=threshold_lon,
				runway_heading_degt=runway_heading_degt,
			)

			if not is_match:
				continue

			# If altitude is known, compute AGL and filter out aircraft
			# that are too high to be near this runway.
			altitude_agl = None
			if isinstance(altitude, (int, float)):
				if (
					threshold_lat == runway["le_latitude_deg"]
					and threshold_lon == runway["le_longitude_deg"]
				):
					end_elevation_ft = runway["le_elevation_ft"]
				else:
					end_elevation_ft = runway["he_elevation_ft"]
				altitude_agl = max(altitude - end_elevation_ft, 0)
				if altitude_agl > 8000:
					# Aircraft is too high to be near this runway.
					continue

			distance_nm = haversine_nm(lat, lon, threshold_lat, threshold_lon)
			range_nm = 15.0
			if altitude_agl is not None:
				# Don't look out any further than the angle of a 3 degree glide slope.
				range_nm = altitude_agl / math.tan(math.radians(3.0)) + 1.0
			if distance_nm > range_nm:
				# Aircraft is too far from this runway threshold.
				continue

			if closest_nm is None or distance_nm < closest_nm:
				closest = runway
				closest_nm = distance_nm
				closest_end_ident = end_ident
				closest_altitude_agl = altitude_agl
				closest_cross_track_nm = cross_track_nm
				closest_track_error_deg = track_error_deg
				closest_closure_nm = closure_nm
				if groundspeed and groundspeed > 0:
					 # Convert hours to seconds
					closest_eta_seconds = (distance_nm / groundspeed) * 3600

	if (
		closest is None
		or closest_nm is None
		or closest_end_ident is None
	):
		return None, None, None, None, None

	logging.debug(
		"Runway centerline match: %s %s/%s end=%s dist=%.2fnm xt=%.2fnm track_err=%.1fdeg closure=%.2fnm",
		closest["airport_ident"],
		closest["le_ident"],
		closest["he_ident"],
		closest_end_ident,
		closest_nm,
		closest_cross_track_nm,
		closest_track_error_deg,
		closest_closure_nm,
	)

	return closest, closest_nm, closest_end_ident, closest_altitude_agl, closest_eta_seconds


def find_nearest_airport(lat, lon):
	if lat is None or lon is None:
		return None, None

	closest = None
	closest_nm = None

	for airport in _airports:
		distance_nm = haversine_nm(lat, lon, airport["lat"], airport["lon"])
		if closest_nm is None or distance_nm < closest_nm:
			closest = airport
			closest_nm = distance_nm

	if closest is None or closest_nm is None:
		return None, None

	return closest, closest_nm


def send_discord_message(title, color, url=None, fields=None, timestamp=None):
	embed = {"title": title, "color": color, "footer": {"text": "ADS-B Exchange"}}
	if url:
		embed["url"] = url
	if fields:
		embed["fields"] = fields
	if timestamp:
		embed["timestamp"] = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
	payload = json.dumps({"embeds": [embed]}).encode()
	if not _discord_webhook_url:
		logging.debug("Discord webhook URL not set; skipping notification: %s", json.dumps(embed, indent=2))
		return
	max_attempts = 6
	retry_delay_seconds = 10

	for attempt in range(1, max_attempts + 1):
		req = Request(_discord_webhook_url, data=payload, method="POST")
		req.add_header("Content-Type", "application/json")
		req.add_header("Accept", "application/json")
		req.add_header("User-Agent", "flightalert/1.0")
		try:
			with urlopen(req, timeout=10):
				logging.debug("Discord webhook post successful")
				return
		except HTTPError as exc:
			logging.warning(
				"Discord webhook post attempt failed HTTP %s (%s)",
				exc.code,
				exc.reason,
			)
		except (URLError, TimeoutError) as exc:
			reason = getattr(exc, "reason", str(exc))
			logging.warning(
				"Discord webhook post attempt failed due to network error: %s",
				reason,
			)
		except Exception as exc:
			logging.warning(
				"Discord webhook post attempt failed: %s",
				exc
			)

		if attempt < max_attempts:
			logging.debug("Retrying Discord webhook post in %ss", retry_delay_seconds)
			time.sleep(retry_delay_seconds)

	logging.error(
		"Discord webhook post failed after %s attempts; skipping notification",
		max_attempts
	)


def emit_transition_event(registration, airport_code, airport_location, icao, transition):
	if transition is None:
		logging.debug("No transition event to emit for %s", registration)
		return

	airport_label = format_airport_label(airport_code, airport_location)
	tracking_url = build_tracking_url(icao)

	# Log the transition.
	log_str = f"{registration} {transition.title()}"
	if airport_label:
		log_str += f" at {airport_label}"
	log_str += f" {tracking_url}"
	logging.info(log_str)

	global _suppress_first_event
	if _suppress_first_event:
		logging.debug("Suppressing %s transition event", transition)
		_suppress_first_event = False
		return

	# Send it to Discord.
	color = _COLOR_GREEN
	if transition == "landing":
		color = _COLOR_BLUE
	fields = []
	if transition == "takeoff" and airport_label:
		fields.append({
			"name": "Departing",
			"value": airport_label,
			"inline": True
		})
	elif transition == "landing" and airport_label:
		fields.append({
			"name": "Arriving",
			"value": airport_label,
			"inline": True
		})
	send_discord_message(
		title=f"\u2708\ufe0f {registration} {transition.title()}",
		color=color,
		url=tracking_url,
		fields=fields or None,
		timestamp=datetime.now(timezone.utc),
	)


def get_transition(current_status, is_near_airport):
	if current_status == "airborne":
		return "takeoff" if is_near_airport else "airborne"
	if current_status == "on_ground":
		return "landing" if is_near_airport else "reset"
	# Should never reach here.
	return None


# Filter out bogus data
# example: alt_baro=3, groundspeed=0.1
# example: alt_baro=41000, groundspeed=24.4
# example: alt_baro="ground", groundspeed=500
def is_airborne(altitude, groundspeed):
	if altitude is None:
		return None
	if isinstance(altitude, str):
		if altitude == "ground":
			if groundspeed is None or groundspeed <= _aircraft_stall_speed:
				return False
		return None
	if not isinstance(altitude, int):
		return None
	if groundspeed is None:
		return None
	if groundspeed > _aircraft_stall_speed:
		return True
	return None


def fetch_snapshot(registration, last_lat, last_lon):
	url = f"{_rapidapi_base_url.rstrip('/')}/registration/{registration}/"
	request = Request(url)
	request.add_header("x-rapidapi-key", _rapidapi_key)
	request.add_header("x-rapidapi-host", _rapidapi_host)
	request.add_header("accept", "application/json")

	try:
		with urlopen(request, timeout=20) as response:
			payload = json.load(response)
	except HTTPError as exc:
		logging.error("Failed to fetch snapshot for %s: HTTP %s %s", registration, exc.code, exc.reason)
		return True, None
	except URLError as exc:
		reason = getattr(exc, "reason", str(exc))
		logging.error("Failed to fetch snapshot for %s: network error: %s", registration, reason)
		return True, None
	except TimeoutError:
		logging.error("Failed to fetch snapshot for %s: request timed out", registration)
		return True, None
	except json.JSONDecodeError as exc:
		logging.error(
			"Failed to fetch snapshot for %s: invalid JSON at line %s column %s",
			registration,
			exc.lineno,
			exc.colno,
		)
		return True, None

	aircraft = payload.get("ac")[0] if payload.get("ac") else None
	if not aircraft:
		logging.debug("No data returned for %s", registration)
		return False, None

	logging.debug(
		"registration=%r icao=%r alt_baro=%r alt_geom=%r groundspeed=%r lat=%r lon=%r track=%r squawk=%r emergency=%r",
		aircraft.get("r"),
		aircraft.get("hex"),
		aircraft.get("alt_baro"),
		aircraft.get("alt_geom"),
		aircraft.get("gs"),
		aircraft.get("lat"),
		aircraft.get("lon"),
		aircraft.get("track"),
		aircraft.get("squawk"),
		aircraft.get("emergency"),
	)

	# Required field, can't proceed without it.
	# aircraft.get("r") is the registration number (tail number) of the aircraft.
	if isinstance(aircraft.get("r"), str) and aircraft.get("r").strip():
		aircraft_r = aircraft.get("r").strip().upper()
	else:
		logging.debug("Invalid registration value %r; skipping snapshot", aircraft.get("r"))
		return True, None

	# Required field, can't proceed without it.
	# aircraft.get("hex") is the ICAO 24-bit address of the aircraft, which is a unique identifier.
	if isinstance(aircraft.get("hex"), str) and aircraft.get("hex").strip():
		icao = aircraft.get("hex").strip().lower()
	else:
		logging.debug("Invalid ICAO value %r; skipping snapshot", aircraft.get("hex"))
		return True, None

	if aircraft.get("lat") is None or aircraft.get("lon") is None:
		lat = None
		lon = None
	else:
		try:
			lat = float(aircraft.get("lat"))
			lon = float(aircraft.get("lon"))
		except (TypeError, ValueError):
			logging.debug(
				"Invalid lat/lon values %r/%r; skipping snapshot",
				aircraft.get("lat"),
				aircraft.get("lon")
			)
			return True, None

	if aircraft.get("gs") is None:
		groundspeed = None
	else:
		try:
			groundspeed = round(float(aircraft.get("gs")))
		except (TypeError, ValueError):
			logging.debug("Invalid groundspeed value %r; skipping snapshot", aircraft.get("gs"))
			return True, None

	if aircraft.get("alt_baro") is None:
		altitude = None
	elif isinstance(aircraft.get("alt_baro"), str):
		altitude = aircraft.get("alt_baro").strip().lower()
	else:
		try:
			altitude = round(float(aircraft.get("alt_baro")))
		except (TypeError, ValueError):
			logging.debug("Invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None

	track = None
	if isinstance(aircraft.get("track"), (int, float)):
		track = float(aircraft.get("track"))

	runway, runway_nm, runway_end_ident, runway_altitude_agl, runway_eta_seconds = find_nearest_runway(
		lat, lon, altitude, track, last_lat, last_lon, groundspeed
	)
	if runway is not None:
		for airport in _airports:
			if airport["ident"] == runway["airport_ident"]:
				logging.debug(
					"Nearest runway: %s %s at %.1fnm, alt_agl=%s, ETA %s",
					airport["ident"],
					runway_end_ident,
					runway_nm,
					format_altitude(runway_altitude_agl),
					format_seconds(runway_eta_seconds)
				)
				break
		else:
			logging.warning("No airport found for runway: %s", runway["airport_ident"])
			runway = None
			runway_nm = None
			runway_end_ident = None
			runway_altitude_agl = None
			runway_eta_seconds = None

	altitude_agl = None
	airport, airport_nm = find_nearest_airport(lat, lon)
	if airport is not None and airport_nm is not None:
		if altitude is None:
			# Cannot determine AGL without altitude.
			pass
		elif isinstance(altitude, int):
			altitude_agl = max(altitude - airport["elevation_ft"], 0)
		elif isinstance(altitude, str) and altitude == "ground":
			# Aircraft is on the ground, so AGL is 0.
			altitude_agl = 0

	airport_code = None
	airport_name = None
	airport_location = None
	if airport is None or airport_nm is None:
		pass
	elif altitude_agl is None or (altitude_agl > NEAR_AIRPORT_AGL_THRESHOLD):
		pass
	elif airport_nm > NEAR_AIRPORT_NM_THRESHOLD:
		pass
	else:
		airport_code = airport["ident"]
		airport_name = airport["name"]
		airport_location = airport["location"]

	logging.debug(
		"Nearest airport: %s (%s) at %.1fnm, alt=%s, alt_agl=%s, is_near_airport=%s",
		airport["ident"],
		airport["location"],
		airport_nm,
		format_altitude(altitude),
		format_altitude(altitude_agl),
		airport_code is not None
	)

	squawk = None
	if isinstance(aircraft.get("squawk"), str):
		squawk = aircraft.get("squawk").strip()

	emergency = None
	if isinstance(aircraft.get("emergency"), str):
		emergency = aircraft.get("emergency").strip().lower()

	return False, FlightSnapshot(
		registration=aircraft_r,
		icao=icao,
		squawk=squawk,
		emergency=emergency,
		altitude=altitude,
		groundspeed=groundspeed,
		lat=lat,
		lon=lon,
		track=track,
		airport_code=airport_code,
		airport_name=airport_name,
		airport_location=airport_location,
		altitude_agl=altitude_agl,
		runway_airport_ident=runway["airport_ident"] if runway else None,
		runway_nm=runway_nm,
		runway_altitude_agl=runway_altitude_agl,
		runway_eta_seconds=runway_eta_seconds,
		runway_end_ident=runway_end_ident
	)


def monitor_plane(registration):
	plane_state = PlaneState()

	while True:
		# Fetch the latest snapshot of the aircraft's state.
		invalid_response, snapshot = fetch_snapshot(
			registration,
			plane_state.last_lat,
			plane_state.last_lon,
		)
		current_status = plane_state.last_status

		if snapshot is None:
			# Aircraft is offline.
			if plane_state.last_status == "airborne":
				if plane_state.last_runway_airport_ident is not None:
					if time_since(plane_state.last_contact) >= plane_state.last_runway_eta_seconds + 60:
						# Aircraft should have landed by now.
						current_status = "on_ground"
				elif plane_state.last_airport_code is not None:
					if time_since(plane_state.last_contact) >= WAIT_FOR_RESET_NEAR_AIRPORT:
						current_status = "on_ground"
				else:
					if time_since(plane_state.last_contact) >= WAIT_FOR_RESET_ENROUTE:
						current_status = "on_ground"
		else:
			# Aircraft is online.
			plane_state.last_contact = datetime.now(timezone.utc)
			if invalid_response:
				pass
			else:
				plane_state.last_registration = snapshot.registration
				plane_state.last_icao = snapshot.icao
				plane_state.last_altitude = snapshot.altitude
				plane_state.last_groundspeed = snapshot.groundspeed
				plane_state.last_lat = snapshot.lat
				plane_state.last_lon = snapshot.lon
				plane_state.last_airport_code = snapshot.airport_code
				plane_state.last_airport_name = snapshot.airport_name
				plane_state.last_airport_location = snapshot.airport_location
				plane_state.last_altitude_agl = snapshot.altitude_agl
				plane_state.last_runway_airport_ident = snapshot.runway_airport_ident
				plane_state.last_runway_end_ident = snapshot.runway_end_ident
				plane_state.last_runway_nm = snapshot.runway_nm
				plane_state.last_runway_altitude_agl = snapshot.runway_altitude_agl
				plane_state.last_runway_eta_seconds = snapshot.runway_eta_seconds
				is_airborne_flag = is_airborne(snapshot.altitude, snapshot.groundspeed)
				if is_airborne_flag is None:
					# Could not determine airborne status; keep the last known status.
					pass
				elif is_airborne_flag is True:
					current_status = "airborne"
				else:
					current_status = "on_ground"

		if plane_state.last_status != current_status:
			# Status has changed, determine the transition type and emit an event.
			logging.debug("Status changed from %s to %s", plane_state.last_status, current_status)
			transition = get_transition(
				current_status=current_status,
				is_near_airport=plane_state.last_airport_code is not None,
			)
			emit_transition_event(
				registration=plane_state.last_registration,
				airport_code=plane_state.last_airport_code,
				airport_location=plane_state.last_airport_location,
				icao=plane_state.last_icao,
				transition=transition
			)
			# Update the status.
			plane_state.last_status = current_status

		# Plane state is now fully updated based on the latest snapshot.
		# Set the next polling interval based on the plane's current status.
		poll_seconds = get_poll_interval(
			plane_state.last_status,
			plane_state.last_altitude_agl,
			plane_state.last_airport_code is not None,
			plane_state.last_contact,
			plane_state.last_poll_seconds
		)
		if plane_state.last_poll_seconds != poll_seconds:
			plane_state.last_poll_seconds = poll_seconds
			logging.debug(
				"Polling interval switched to %s (alt %s, agl %s, state %s)",
				poll_seconds,
				format_altitude(plane_state.last_altitude),
				format_altitude(plane_state.last_altitude_agl),
				plane_state.last_status
			)
		logging.debug("Sleeping for %s before next poll", plane_state.last_poll_seconds)
		time.sleep(plane_state.last_poll_seconds)


def main():
	if len(sys.argv) < 2:
		raise SystemExit("Usage: python tracker.py <REGISTRATION> [--suppress-first-event]")
	registration = sys.argv[1].strip().upper()
	if not registration:
		raise SystemExit("REGISTRATION argument cannot be empty")

	global _rapidapi_key, _rapidapi_host, _rapidapi_base_url
	_rapidapi_key = os.getenv("RAPIDAPI_KEY")
	if not _rapidapi_key:
		logging.error("RAPIDAPI_KEY not set; cannot fetch aircraft data")
		raise SystemExit(1)
	_rapidapi_host = "adsbexchange-com1.p.rapidapi.com"
	_rapidapi_base_url = "https://adsbexchange-com1.p.rapidapi.com/v2"

	global _discord_webhook_url
	_discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
	if not _discord_webhook_url:
		logging.warning("DISCORD_WEBHOOK_URL not set; Discord notifications disabled")

	load_airports(AIRPORTS_CSV_LOCAL_PATH)
	load_runways(RUNWAYS_CSV_LOCAL_PATH)

	#raise SystemExit(0)

	logging.info("Starting tracker for registration %s", registration)
	if len(sys.argv) >= 3 and sys.argv[2].strip().lower() == "--suppress-first-event":
		global _suppress_first_event
		_suppress_first_event = True
		logging.info("The first transition event will be suppressed")
	
	try:
		monitor_plane(registration)
	except KeyboardInterrupt:
		logging.info("Tracker interrupted by user; exiting")
		raise SystemExit(0)
	except Exception as exc:
		logging.exception("Unexpected error occurred: %s", exc)
		raise SystemExit(1)


if __name__ == "__main__":
	main()
