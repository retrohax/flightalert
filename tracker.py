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

# List of airports loaded from airports.csv
_airports = []


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
	altitude: int | None
	groundspeed: float | None
	lat: float | None
	lon: float | None
	track: float | None
	airport_code: str | None = None
	airport_location: str | None = None
	altitude_agl: int | None = None


@dataclass
class PlaneState:
	last_registration: str = ""
	last_icao: str = ""
	last_altitude: int | None = None
	last_altitude_agl: int | None = None
	last_groundspeed: float | None = None
	last_lat: float | None = None
	last_lon: float | None = None
	last_airport_code: str | None = None
	last_airport_location: str | None = None
	last_altitude_agl: int | None = None
	last_status: str = "on_ground"
	last_poll_seconds: int = 0
	last_contact: datetime = field(default_factory=lambda: datetime.fromtimestamp(0, timezone.utc))


def build_tracking_url(icao):
	return f"https://globe.adsbexchange.com/?icao={quote(icao)}"


def format_altitude(altitude):
	return f"{int(round(altitude))}ft" if altitude is not None else "unknown"


def format_groundspeed(groundspeed):
	return f"{int(round(groundspeed))}kt" if groundspeed is not None else "unknown"


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


def get_poll_interval(status, altitude, is_near_airport, last_contact, last_poll_seconds):
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
		if altitude > POLL_THRESHOLD_ALTITUDE_BC:
			return POLL_SECONDS_ALTITUDE_C
		if altitude > POLL_THRESHOLD_ALTITUDE_AB:
			return POLL_SECONDS_ALTITUDE_B
		return POLL_SECONDS_ALTITUDE_A
	# Should never reach here.
	return POLL_SECONDS_OFFLINE


def load_airports():
	global _airports
	_airports = []

	try:
		with open(AIRPORTS_CSV_LOCAL_PATH, "r", encoding="utf-8") as f:
			csv_text = f.read()
	except FileNotFoundError:
		logging.warning("Local airports CSV not found")
		raise SystemExit(1)

	reader = csv.DictReader(io.StringIO(csv_text))

	for row in reader:
		if row.get("type") not in AIRPORT_TYPES:
			continue

		try:
			elevation_ft = int(row.get("elevation_ft"))
		except (TypeError, ValueError):
			continue

		try:
			lat = float(row.get("latitude_deg"))
			lon = float(row.get("longitude_deg"))
		except (TypeError, ValueError):
			continue

		code = row.get("icao_code") or row.get("ident") or "UNKNOWN"
		code = str(code).strip().upper() if code else "UNKNOWN"

		municipality = row.get("municipality") if isinstance(row.get("municipality"), str) else ""
		country = row.get("iso_country") if isinstance(row.get("iso_country"), str) else ""
		location = ", ".join(part for part in [municipality, country] if part) or "unknown location"

		_airports.append(
			{
				"code": code,
				"location": location,
				"lat": lat,
				"lon": lon,
				"elevation_ft": elevation_ft,
			}
		)

	logging.debug("Loaded %d airports for takeoff/landing detection", len(_airports))

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


def find_nearest_airport(lat, lon):
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


def is_airborne(altitude_agl, groundspeed):
	if altitude_agl > 100 and groundspeed > 25.0:
		# We need to guard against fluttering where aircraft is actually
		# on the ground but sending wonky data. This basic sanity check
		# helps prevent false takeoff/landing events.
		return True
	return False


def fetch_snapshot(registration):
	url = f"{_rapidapi_base_url.rstrip('/')}/registration/{registration}/"
	request = Request(url)
	request.add_header("x-rapidapi-key", _rapidapi_key)
	request.add_header("x-rapidapi-host", _rapidapi_host)
	request.add_header("accept", "application/json")

	try:
		with urlopen(request, timeout=20) as response:
			payload = json.load(response)
	except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
		logging.error("Failed to fetch snapshot")
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

	if isinstance(aircraft.get("r"), str) and aircraft.get("r").strip():
		aircraft_r = aircraft.get("r").strip().upper()
	else:
		logging.debug("Invalid registration value %r; skipping snapshot", aircraft.get("r"))
		return True, None

	if isinstance(aircraft.get("hex"), str) and aircraft.get("hex").strip():
		icao = aircraft.get("hex").strip().lower()
	else:
		logging.debug("Invalid ICAO value %r; skipping snapshot", aircraft.get("hex"))
		return True, None

	try:
		groundspeed = float(aircraft.get("gs"))
	except:
		logging.debug("Invalid groundspeed value %r; skipping snapshot", aircraft.get("gs"))
		return True, None

	try:
		lat = float(aircraft.get("lat"))
		lon = float(aircraft.get("lon"))
	except:
		logging.debug("Invalid lat/lon values %r/%r; skipping snapshot", aircraft.get("lat"), aircraft.get("lon"))
		return True, None

	altitude = None
	if isinstance(aircraft.get("alt_baro"), str):
		if aircraft.get("alt_baro").strip().lower() == "ground":
			pass
		else:
			logging.debug("invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None
	else:
		try:
			altitude = round(aircraft.get("alt_baro"))
		except:
			logging.debug("invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None
	if altitude is not None:
		# See if we can upgrade from alt_baro to alt_geom if available.
		if isinstance(aircraft.get("alt_geom"), (int, float)):
			altitude = round(aircraft.get("alt_geom"))

	altitude_agl = None
	airport, airport_nm = find_nearest_airport(lat, lon)
	if airport is not None and airport_nm is not None:
		altitude_agl = 0
		if altitude is None:
			# alt_baro is "ground", aircraft is on the ground.
			pass
		else:
			altitude_agl = max(altitude - airport["elevation_ft"], 0)

	if altitude_agl is None:
		logging.debug("Cannot determine nearest airport; skipping snapshot")
		return True, None

	airport_code = None
	airport_location = None
	if airport is None or airport_nm is None:
		pass
	elif altitude_agl > NEAR_AIRPORT_AGL_THRESHOLD:
		pass
	elif airport_nm > NEAR_AIRPORT_NM_THRESHOLD:
		pass
	else:
		airport_code = airport["code"]
		airport_location = airport["location"]

	logging.debug(
		"Nearest airport: %s (%s) at %.1fnm, alt_agl=%s, is_near_airport=%s",
		airport["code"],
		airport["location"],
		airport_nm,
		format_altitude(altitude_agl),
		airport_code is not None
	)

	track = None
	if isinstance(aircraft.get("track"), (int, float)):
		track = float(aircraft.get("track"))

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
		airport_location=airport_location,
		altitude_agl=altitude_agl
	)


def monitor_plane(registration):
	plane_state = PlaneState()

	while True:
		# Fetch the latest snapshot of the aircraft's state.
		invalid_response, snapshot = fetch_snapshot(registration)
		current_status = plane_state.last_status

		if snapshot is None:
			# Aircraft is offline.
			if plane_state.last_status == "airborne":
				if plane_state.last_airport_code is not None:
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
				plane_state.last_airport_location = snapshot.airport_location
				plane_state.last_altitude_agl = snapshot.altitude_agl
				if is_airborne(snapshot.altitude_agl, snapshot.groundspeed):
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
			plane_state.last_altitude_agl or plane_state.last_altitude or 0,
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
	if len(sys.argv) != 2:
		raise SystemExit("Usage: python tracker.py <REGISTRATION>")
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

	load_airports()

	logging.info("Starting tracker for registration %s", registration)
	monitor_plane(registration)


if __name__ == "__main__":
	main()
