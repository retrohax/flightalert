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
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

AIRPORTS_CSV_LOCAL_PATH = "airports.csv"
AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}

POLL_SECONDS_OFFLINE = 900
POLL_SECONDS_ON_GROUND = 10
POLL_SECONDS_LOW_ALTITUDE = 10
POLL_SECONDS_ENROUTE_ALTITUDE = 300
POLL_THRESHOLD_ALTITUDE = 10000
POLL_SECONDS_TO_WAIT_FOR_OFFLINE = 300

# If an aircraft is within this distance and altitude of an airport,
# we'll consider it "near" that airport.
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
	is_airborne: bool
	timestamp: datetime


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
	last_state: str = "offline"
	last_state_timestamp: datetime = datetime.now(timezone.utc)
	last_poll_seconds: int = POLL_SECONDS_OFFLINE
	previous_flight_phase: str = "landing"


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


def set_poll_interval(state):
	poll_interval = state.last_poll_seconds

	if state.last_state == 'offline':
		time_since_last_state = (datetime.now(timezone.utc) - state.last_state_timestamp).total_seconds()
		if time_since_last_state >= POLL_SECONDS_TO_WAIT_FOR_OFFLINE:
			poll_interval = POLL_SECONDS_OFFLINE
	elif state.last_state == 'on_ground':
		poll_interval = POLL_SECONDS_ON_GROUND
	elif state.last_state == 'airborne':
		if state.last_altitude > POLL_THRESHOLD_ALTITUDE:
			poll_interval = POLL_SECONDS_ENROUTE_ALTITUDE
		else:
			poll_interval = POLL_SECONDS_LOW_ALTITUDE

	if state.last_poll_seconds != poll_interval:
		state.last_poll_seconds = poll_interval
		logging.debug(
			"Polling interval switched to %ss (alt %s, state %s)",
			poll_interval,
			format_altitude(state.last_altitude),
			state.last_state or "unknown",
		)


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


def find_airport(lat, lon, altitude):
	closest = None
	closest_nm = None

	for airport in _airports:
		distance_nm = haversine_nm(lat, lon, airport["lat"], airport["lon"])
		if closest_nm is None or distance_nm < closest_nm:
			closest = airport
			closest_nm = distance_nm

	if closest is None or closest_nm is None:
		return None, None

	altitude_agl = 0
	if altitude > 0:
		# altitude > 0 means alt_baro is a number, not "ground"
		altitude_agl = altitude - closest["elevation_ft"]

	logging.debug("Airport: %s (%s) at %.1fnm, alt_agl=%s",
		closest["code"],
		closest["location"],
		closest_nm,
		format_altitude(altitude_agl),
	)

	if altitude_agl > NEAR_AIRPORT_AGL_THRESHOLD:
		return None, None

	if closest_nm > NEAR_AIRPORT_NM_THRESHOLD:
		return None, None

	logging.debug("Aircraft is near airport %s (%s)", closest["code"], closest["location"])

	return closest["code"], closest["location"]


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


def emit_transition_event(state, transition, timestamp, detection_method):
	airport_label = format_airport_label(state.last_airport_code, state.last_airport_location)
	tracking_url = build_tracking_url(state.last_icao)

	# Log the transition no matter what it is.
	log_str = f"{state.last_registration} {transition.title()} ({detection_method})"
	if airport_label:
		log_str += f" at {airport_label}"
	log_str += f" {tracking_url}"
	logging.info(log_str)

	# The only transitions we send to Discord are TAKEOFF, AIRBORNE, and LANDING.
	# We'll only send one AIRBORNE per flight and only if we haven't already had a TAKEOFF.
	if transition == "takeoff":
		state.previous_flight_phase = "takeoff"
	elif transition == "airborne":
		if state.previous_flight_phase in {"takeoff", "airborne"}:
			return
		state.previous_flight_phase = "airborne"
	elif transition == "landing":
		state.previous_flight_phase = "landing"
	else:
		return

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
		title=f"\u2708\ufe0f {state.last_registration} {transition.title()}",
		color=color,
		url=tracking_url,
		fields=fields or None,
		timestamp=timestamp,
	)


def process_transition(state, current_state, timestamp):
	if not state.last_state in {"offline", "on_ground", "airborne"}:
		logging.warning("Unexpected last_state value: %s", state.last_state)
		return
	if not current_state in {"offline", "on_ground", "airborne"}:
		logging.warning("Unexpected current_state value: %s", current_state)
		return
	if state.last_state == current_state:
		return
	state.last_state_timestamp = datetime.now(timezone.utc)

	transition = None
	detection_method = "confirmed"
	if current_state == "offline" and state.last_state == "on_ground":
		transition = "offline"
	if current_state == "offline" and state.last_state == "airborne":
		if state.last_airport_code is not None:
			transition = "landing"
			detection_method = "assumed"
		else:
			transition = "offline"
	if current_state == "on_ground" and state.last_state == "airborne":
		transition = "landing"
	if current_state == "on_ground" and state.last_state == "offline":
		transition = "online"
	if current_state == "airborne" and state.last_state == "on_ground":
		transition = "takeoff"
	if current_state == "airborne" and state.last_state == "offline":
		if state.last_airport_code is not None:
			transition = "takeoff"
			detection_method = "assumed"
		else:
			transition = "airborne"

	if transition is None:
		logging.warning("Unexpected transition from %s to %s", state.last_state, current_state)
		return

	emit_transition_event(
		state,
		transition,
		timestamp,
		detection_method=detection_method,
	)

	return None


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
		logging.debug("No data returned")
		return False, None

	logging.debug(
		"registration=%r icao=%r alt_baro=%r groundspeed=%r lat=%r lon=%r",
		aircraft.get("r"),
		aircraft.get("hex"),
		aircraft.get("alt_baro"),
		aircraft.get("gs"),
		aircraft.get("lat"),
		aircraft.get("lon"),
	)

	#logging.debug("Raw aircraft data: %s", json.dumps(aircraft, indent=2))

	if isinstance(aircraft.get("r"), str) and aircraft.get("r").strip():
		aircraft_r = aircraft.get("r").strip().upper()
	else:
		logging.debug("Invalid registration value %r; skipping snapshot", aircraft.get("r"))
		return True, None

	if isinstance(aircraft.get("hex"), str) and aircraft.get("hex").strip():
		icao = aircraft.get("hex").strip().upper()
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

	altitude = 0
	is_airborne = False
	if isinstance(aircraft.get("alt_baro"), str):
		if aircraft.get("alt_baro").strip().lower() != "ground":
			logging.debug("invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None
	else:
		try:
			altitude = round(aircraft.get("alt_baro"))
		except:
			logging.debug("invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None
		# sanity check, sometimes ads-b data is wonky
		if altitude > 0 and groundspeed > 25.0:
			is_airborne = True

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
		is_airborne=is_airborne,
		timestamp=datetime.now(timezone.utc),
	)


def monitor_plane(registration):
	state = PlaneState()

	while True:
		invalid_response, snapshot = fetch_snapshot(registration)

		if invalid_response:
			pass
		else:
			if snapshot is None:
				# no data returned; treat as offline
				current_state = "offline"
				current_timestamp = datetime.now(timezone.utc)
			else:
				state.last_registration = snapshot.registration
				state.last_icao = snapshot.icao
				state.last_altitude = snapshot.altitude
				state.last_groundspeed = snapshot.groundspeed
				state.last_lat = snapshot.lat
				state.last_lon = snapshot.lon
				state.last_airport_code, state.last_airport_location = find_airport(
					snapshot.lat,
					snapshot.lon,
					snapshot.altitude,
				)
				current_state = "airborne" if snapshot.is_airborne else "on_ground"
				current_timestamp = snapshot.timestamp

			process_transition(state, current_state, current_timestamp)
			state.last_state = current_state

		set_poll_interval(state)
		logging.debug("Sleeping for %s before next poll", state.last_poll_seconds)
		time.sleep(state.last_poll_seconds)


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
