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
# 2) Download the runways.csv file:
# wget https://davidmegginson.github.io/ourairports-data/runways.csv
# 
# 3) Set the RAPIDAPI_KEY environment variable:
# export RAPIDAPI_KEY="your_rapidapi_key"
#
# 4) Set the DISCORD_WEBHOOK_URL environment variable:
# export DISCORD_WEBHOOK_URL="your_discord_webhook_url"
#
# NOTE: Discord is optional; if you don't set the webhook URL,
# the script will still run and log events to stdout.
#
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from airports import (
	AirportInfo,
	RunwayInfo,
	RunwaySearchStatus,
	find_nearest_airport,
	find_airport_by_ident,
	find_nearest_runway,
	load_airports,
	load_runways,
)
from discord import send_discord_message

AIRPORTS_CSV_LOCAL_PATH = "airports.csv"

RUNWAYS_CSV_LOCAL_PATH = "runways_vabb.csv"

# We only get 10,000 api requests per month so we limit the polling
# frequency where we can without losing transition detection accuracy.
POLL_SECONDS_OFFLINE = 900
POLL_SECONDS_ON_GROUND = 30
POLL_SECONDS_ALTITUDE_A = 30
POLL_SECONDS_ALTITUDE_B = 300
POLL_SECONDS_ALTITUDE_C = 900
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
	airport: AirportInfo | None = None
	altitude_agl: int | None = None
	runway_eta_seconds: int | None = None
	runway_status: RunwaySearchStatus | None = None
	runway: RunwayInfo | None = None


@dataclass
class PlaneState:
	last_registration: str = ""
	last_icao: str = ""
	last_altitude: str | int | None = None
	last_groundspeed: int | None = None
	last_lat: float | None = None
	last_lon: float | None = None
	last_airport: AirportInfo | None = None
	last_altitude_agl: int | None = None
	last_runway_eta_seconds: int | None = None
	last_runway: RunwayInfo | None = None
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


def get_poll_interval(status, altitude_agl, last_contact, last_poll_seconds):
	if status == 'on_ground':
		if time_since(last_contact) >= WAIT_FOR_OFFLINE:
			return POLL_SECONDS_OFFLINE
		return POLL_SECONDS_ON_GROUND
	if status == 'airborne':
		if altitude_agl is None:
			return last_poll_seconds
		if altitude_agl > POLL_THRESHOLD_ALTITUDE_BC:
			return POLL_SECONDS_ALTITUDE_C
		if altitude_agl > POLL_THRESHOLD_ALTITUDE_AB:
			return POLL_SECONDS_ALTITUDE_B
		return POLL_SECONDS_ALTITUDE_A
	# Should never reach here.
	return POLL_SECONDS_OFFLINE


def emit_transition_event(registration, airport, icao, transition):
	if transition is None:
		logging.debug("No transition event to emit for %s", registration)
		return

	airport_label = format_airport_label(airport.ident if airport else None, airport.location if airport else None)
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
	send_discord_message(		webhook_url=_discord_webhook_url,		title=f"\u2708\ufe0f {registration} {transition.title()}",
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
	if altitude is None or isinstance(altitude, int):
		if aircraft.get("alt_geom") is not None:
			try:
				altitude = round(float(aircraft.get("alt_geom")))
			except (TypeError, ValueError):
				pass  # Ignore invalid alt_geom, keep alt_baro value.

	track = None
	if isinstance(aircraft.get("track"), (int, float)):
		track = float(aircraft.get("track"))

	airport_info = None
	altitude_agl = None
	runway_eta_seconds = None
	runway_search = find_nearest_runway(lat, lon, altitude, track, last_lat, last_lon)
	logging.debug(
		"Runway search result: lat=%s lon=%s altitude=%s track=%s last_lat=%s last_lon=%s status=%s runway=%s",
		lat,
		lon,
		altitude,
		track,
		last_lat,
		last_lon,
		runway_search.status,
		runway_search.runway.end_ident if runway_search.runway else None
	)

	if runway_search.status == RunwaySearchStatus.FOUND:
		airport_info = find_airport_by_ident(runway_search.runway.airport_ident)
		if isinstance(altitude, int):
			altitude_agl = altitude - runway_search.runway.end_elevation_ft
		if groundspeed is not None:
			runway_eta_seconds = round((runway_search.runway.nm / groundspeed) * 3600)
	else:
		airport, airport_nm = find_nearest_airport(lat, lon)
		if airport is not None:
			if isinstance(altitude, int):
				altitude_agl = altitude - airport.elevation_ft
			if (
				isinstance(altitude, str) and altitude == "ground"
				or (altitude_agl is not None
				and altitude_agl <= NEAR_AIRPORT_AGL_THRESHOLD
				and airport_nm <= NEAR_AIRPORT_NM_THRESHOLD)
			):
				airport_info = airport

	if airport_info is not None:
		logging.debug(
			"Airport: %s (%s) at %.1fnm, alt=%s, alt_agl=%s, gs=%s, runway_eta=%s",
			airport_info.ident,
			airport_info.location,
			airport_nm,
			format_altitude(altitude),
			format_altitude(altitude_agl),
			groundspeed,
			runway_eta_seconds
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
		airport=airport_info,
		altitude_agl=altitude_agl,
		runway_eta_seconds=runway_eta_seconds,
		runway_status=runway_search.status,
		runway=runway_search.runway,
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
				if plane_state.last_runway is not None and plane_state.last_runway_eta_seconds is not None:
					if time_since(plane_state.last_contact) >= plane_state.last_runway_eta_seconds + 60:
						# Aircraft should have landed by now.
						current_status = "on_ground"
				elif plane_state.last_airport is not None:
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
				plane_state.last_airport = snapshot.airport
				plane_state.last_altitude_agl = snapshot.altitude_agl
				if snapshot.runway_status == RunwaySearchStatus.FOUND:
					plane_state.last_runway = snapshot.runway
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
				is_near_airport=plane_state.last_airport is not None,
			)
			emit_transition_event(
				registration=plane_state.last_registration,
				airport=plane_state.last_airport,
				icao=plane_state.last_icao,
				transition=transition
			)
			# Update the status.
			plane_state.last_status = current_status
			plane_state.last_runway = None
			plane_state.last_runway_eta_seconds = None

		# Plane state is now fully updated based on the latest snapshot.
		# Set the next polling interval based on the plane's current status.
		poll_seconds = get_poll_interval(
			plane_state.last_status,
			plane_state.last_altitude_agl,
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
