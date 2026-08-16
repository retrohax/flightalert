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
import logging
import os
import sys
import time
from enum import Enum
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import quote

from airports import (
	AirportSearchStatus,
	AirportSearchResult,
	find_glideslope_airport,
	find_runway_airport,
	find_nearest_airport,
	load_airports,
	load_runways,
)

from discord import send_discord_message
from rapidapi import rapidapi_request

_LOG_LEVEL = logging.INFO
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

AIRPORTS_CSV_LOCAL_PATH = "airports.csv"
RUNWAYS_CSV_LOCAL_PATH = "runways.csv"

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

# RapidAPI
_rapidapi_key = None
_rapidapi_host = None
_rapidapi_base_url = None

# Discord
_discord_webhook_url = None
_COLOR_GREEN  = 3066993   # takeoff, airborne
_COLOR_BLUE   = 3447003   # landing

_aircraft_stall_speed = 100.0  # knots

# Useful when starting mid-flight to avoid emitting an airborne event.
_suppress_first_event = False

_replay_log = None
_replay_timestamp = None

_SKIPPED_AIRPORT_RESULT = AirportSearchResult(
	status=AirportSearchStatus.SKIPPED,
	timeout_seconds=0,
)


class ReplayTimestampFilter(logging.Filter):
	def filter(self, record):
		if _replay_log is None or _replay_timestamp is None:
			return True
		if getattr(record, "_replay_tagged", False):
			return True
		replay_local = _replay_timestamp.astimezone()
		record.created = replay_local.timestamp()
		record.msecs = replay_local.microsecond / 1000
		record.relativeCreated = (record.created - logging._startTime) * 1000
		record._replay_tagged = True
		return True

logging.basicConfig(
	level=_LOG_LEVEL,
	format=_LOG_FORMAT,
	datefmt=_LOG_DATEFMT,
)
logging.getLogger().addFilter(ReplayTimestampFilter())


@dataclass
class Snapshot:
	registration: str
	icao: str
	squawk: str | None
	emergency: str | None
	altitude: str | int | None
	altitude_agl: int | None
	groundspeed: int | None
	lat: float | None
	lon: float | None
	track: float | None
	runway_airport: AirportSearchResult
	glideslope_airport: AirportSearchResult
	local_airport: AirportSearchResult


@dataclass
class PlaneState:
	last_registration: str = ""
	last_icao: str = ""
	last_altitude: str | int | None = None
	last_altitude_agl: int | None = None
	last_groundspeed: int | None = None
	last_lat: float | None = None
	last_lon: float | None = None
	last_status: str = "on_ground"
	last_poll_seconds: int = POLL_SECONDS_ON_GROUND
	last_contact: datetime = field(default_factory=lambda: datetime.fromtimestamp(0, timezone.utc))
	last_runway_airport: AirportSearchResult = field(default_factory=lambda: _SKIPPED_AIRPORT_RESULT)
	last_glideslope_airport: AirportSearchResult = field(default_factory=lambda: _SKIPPED_AIRPORT_RESULT)


class SnapshotStatus(str, Enum):
	ERROR = "error"
	ONLINE = "online"
	OFFLINE = "offline"


@dataclass(frozen=True)
class SnapshotResult:
	status: SnapshotStatus
	snapshot: Snapshot | None = None


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


def datetime_now():
	if _replay_log is not None:
		return _replay_timestamp
	return datetime.now(timezone.utc)


def time_since(start_time):
	return (datetime_now() - start_time).total_seconds()


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
	send_discord_message(
		webhook_url=_discord_webhook_url,
		title=f"\u2708\ufe0f {registration} {transition.title()}",
		color=color,
		url=tracking_url,
		fields=fields or None,
		timestamp=datetime_now(),
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
def is_airborne(altitude, groundspeed, is_near_airport):
	if altitude is None:
		return None
	if isinstance(altitude, str):
		if altitude == "ground" and is_near_airport:
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


def get_next_replay_log_entry():
	global _replay_timestamp

	while True:
		log_entry = _replay_log.readline()
		if log_entry == "":
			logging.debug("End of replay log reached")
			_replay_log.close()
			raise SystemExit(0)

		log_entry = log_entry.rstrip("\n")
		parts = log_entry.split(" ", 2)
		if len(parts) < 3:
			continue

		timestamp_str = parts[0]
		try:
			timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
		except ValueError:
			continue

		if timestamp.tzinfo is None:
			continue

		log_level = parts[1]
		message = parts[2]
		if log_level != "DEBUG":
			continue

		payload = {}
		_replay_timestamp = timestamp.astimezone(timezone.utc)

		if message.startswith("No data returned"):
			return payload

		if not message.startswith("registration="):
			continue

		for pair in message.split():
			if "=" not in pair:
				continue
			key, value = pair.split("=", 1)
			if value.startswith("'") and value.endswith("'"):
				value = value[1:-1]
			else:
				try:
					value = float(value)
				except ValueError:
					pass
			key = "r" if key == "registration" else key
			key = "hex" if key == "icao" else key
			key = "alt_baro" if key == "alt_baro" else key
			key = "alt_geom" if key == "alt_geom" else key
			key = "gs" if key == "groundspeed" else key
			key = "lat" if key == "lat" else key
			key = "lon" if key == "lon" else key
			key = "track" if key == "track" else key
			key = "squawk" if key == "squawk" else key
			key = "emergency" if key == "emergency" else key
			value = None if value == "None" else value
			payload[key] = value

		return {"ac": [payload]}


def touchdown_eta_seconds(distance_nm, groundspeed):
	if distance_nm is None or distance_nm <= 0:
		return 0
	if groundspeed is None or groundspeed <= 0:
		return 0
	final_leg_gs = _aircraft_stall_speed * 1.15
	approach_leg_gs = (groundspeed - final_leg_gs) / 2 + final_leg_gs
	approach_leg_nm = max(distance_nm - 4.0, 0)
	approach_leg_hours = approach_leg_nm / approach_leg_gs
	final_leg_nm = min(distance_nm, 4.0)
	final_leg_hours = final_leg_nm / final_leg_gs
	return round((approach_leg_hours + final_leg_hours) * 3600)


def fetch_snapshot(registration, last_lat, last_lon, last_altitude, last_contact):
	if _replay_log is not None:
		payload = get_next_replay_log_entry()
	else:
		payload = rapidapi_request(
			_rapidapi_base_url,
			_rapidapi_host,
			_rapidapi_key,
			registration
		)
	if payload is None:
		return SnapshotResult(status=SnapshotStatus.ERROR)

	aircraft = payload.get("ac")[0] if payload.get("ac") else None
	if not aircraft:
		logging.debug("No data returned for %s", registration)
		return SnapshotResult(status=SnapshotStatus.OFFLINE)

	logging.debug(
		"registration=%r icao=%r alt_baro=%r alt_geom=%r gs=%r lat=%r lon=%r track=%r squawk=%r emergency=%r",
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
		return SnapshotResult(status=SnapshotStatus.ONLINE, snapshot=None)

	if isinstance(aircraft.get("hex"), str) and aircraft.get("hex").strip():
		icao = aircraft.get("hex").strip().lower()
	else:
		logging.debug("Invalid ICAO value %r; skipping snapshot", aircraft.get("hex"))
		return SnapshotResult(status=SnapshotStatus.ONLINE, snapshot=None)

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
			return SnapshotResult(status=SnapshotStatus.ONLINE, snapshot=None)

	if aircraft.get("gs") is None:
		groundspeed = None
	else:
		try:
			groundspeed = round(float(aircraft.get("gs")))
		except (TypeError, ValueError):
			logging.debug("Invalid groundspeed value %r; skipping snapshot", aircraft.get("gs"))
			return SnapshotResult(status=SnapshotStatus.ONLINE, snapshot=None)

	if aircraft.get("alt_baro") is None:
		altitude = None
	elif isinstance(aircraft.get("alt_baro"), str):
		altitude = aircraft.get("alt_baro").strip().lower()
	else:
		try:
			altitude = round(float(aircraft.get("alt_baro")))
		except (TypeError, ValueError):
			logging.debug("Invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return SnapshotResult(status=SnapshotStatus.ONLINE, snapshot=None)
	if altitude is None or isinstance(altitude, int):
		if aircraft.get("alt_geom") is not None:
			try:
				altitude = round(float(aircraft.get("alt_geom")))
			except (TypeError, ValueError):
				pass  # Ignore invalid alt_geom, keep alt_baro value.

	if (
		isinstance(altitude, int)
		and altitude == last_altitude
		and lat == last_lat
		and lon == last_lon
	):
		logging.debug("Stuck aircraft position data in flight; skipping snapshot")
		return SnapshotResult(status=SnapshotStatus.ONLINE, snapshot=None)

	track = None
	if isinstance(aircraft.get("track"), (int, float)):
		track = float(aircraft.get("track"))

	squawk = None
	if isinstance(aircraft.get("squawk"), str):
		squawk = aircraft.get("squawk").strip()

	emergency = None
	if isinstance(aircraft.get("emergency"), str):
		emergency = aircraft.get("emergency").strip().lower()

	# Use the field elevation of the nearest airport to calculate altitude AGL (Above Ground Level).
	# This will be used by the polling interval logic to determine how frequently to poll for updates.
	altitude_agl = None	
	nearest_airport = find_nearest_airport(lat, lon, altitude)
	if nearest_airport.status == AirportSearchStatus.FOUND:
		altitude_agl = nearest_airport.altitude_agl
		logging.debug(
			"Nearest airport: %s (%s) at %.1fnm, altitude_agl=%s",
			nearest_airport.airport.ident,
			nearest_airport.airport.location,
			nearest_airport.distance_nm,
			format_altitude(nearest_airport.altitude_agl),
		)

	descent_rate = None
	if isinstance(altitude, int) and isinstance(last_altitude, int) and last_contact is not None:
		descent_rate = round((last_altitude - altitude) / (time_since(last_contact) / 60))
		if altitude_agl is not None and altitude_agl <= 10000:
			logging.debug("Descent rate below 10,000ft: %dfpm", descent_rate)

	eta_seconds = 0
	runway_airport = find_runway_airport(lat, lon, altitude, track, descent_rate)
	if runway_airport.status == AirportSearchStatus.FOUND:
		eta_seconds = touchdown_eta_seconds(runway_airport.distance_nm, groundspeed)
		logging.debug(
			"Runway airport: %s (%s) at %.1fnm, eta: %ss",
			runway_airport.airport.ident,
			runway_airport.airport.location,
			runway_airport.distance_nm,
			eta_seconds
		)
	runway_airport = replace(runway_airport, timeout_seconds=eta_seconds, timestamp=datetime_now())

	eta_seconds = 0
	glideslope_airport = find_glideslope_airport(lat, lon, track, altitude, descent_rate)
	if glideslope_airport.status == AirportSearchStatus.FOUND:
		eta_seconds = touchdown_eta_seconds(glideslope_airport.distance_nm, groundspeed)
		logging.debug(
			"Glideslope airport: %s (%s) at %.1fnm, eta: %ss",
			glideslope_airport.airport.ident,
			glideslope_airport.airport.location,
			glideslope_airport.distance_nm,
			eta_seconds
		)
	glideslope_airport = replace(glideslope_airport, timeout_seconds=eta_seconds, timestamp=datetime_now())

	local_airport = find_nearest_airport(lat, lon, altitude, find_local=True)
	if local_airport.status == AirportSearchStatus.FOUND:
		logging.debug(
			"Local airport: %s (%s) at %.1fnm",
			local_airport.airport.ident,
			local_airport.airport.location,
			local_airport.distance_nm,
		)

	return SnapshotResult(
		status=SnapshotStatus.ONLINE,
		snapshot=Snapshot(
			registration=aircraft_r,
			icao=icao,
			squawk=squawk,
			emergency=emergency,
			altitude=altitude,
			altitude_agl=altitude_agl,
			groundspeed=groundspeed,
			lat=lat,
			lon=lon,
			track=track,
			glideslope_airport=glideslope_airport,
			runway_airport=runway_airport,
			local_airport=local_airport,
		)
	)


def monitor_plane(registration):
	plane_state = PlaneState()

	while True:
		# Fetch the latest snapshot of the aircraft's state.
		snapshot_result = fetch_snapshot(
			registration,
			plane_state.last_lat,
			plane_state.last_lon,
			plane_state.last_altitude,
			plane_state.last_contact
		)
		# Save status so we can see if it changed after processing the snapshot.
		current_status = plane_state.last_status
		local_airport = None

		if snapshot_result.status == SnapshotStatus.ERROR:
			# Probably some kind of network error.
			pass
		elif snapshot_result.status == SnapshotStatus.OFFLINE:
			# Aircraft is offline.
			if plane_state.last_status == "airborne":
				# We might need to assume a landing.

				last_contact_seconds = time_since(plane_state.last_contact)
				timeout_seconds = max(
					plane_state.last_runway_airport.timeout_seconds,
					plane_state.last_glideslope_airport.timeout_seconds,
				)

				if (
					timeout_seconds > 0
					and plane_state.last_runway_airport.status == AirportSearchStatus.FOUND
					and plane_state.last_runway_airport.timestamp is not None
					and time_since(plane_state.last_runway_airport.timestamp) > timeout_seconds
				):
					current_status = "on_ground"
					local_airport = plane_state.last_runway_airport
					logging.debug(
						"Assuming last_runway_airport landing at %s due to timeout (last contact %.1f seconds ago)",
						local_airport.airport.ident,
						last_contact_seconds,
					)

				elif (
					timeout_seconds > 0
					and plane_state.last_glideslope_airport.status == AirportSearchStatus.FOUND
					and plane_state.last_glideslope_airport.timestamp is not None
					and time_since(plane_state.last_glideslope_airport.timestamp) > timeout_seconds
				):
					current_status = "on_ground"
					local_airport = plane_state.last_glideslope_airport
					logging.debug(
						"Assuming last_glideslope_airport landing at %s due to timeout (last contact %.1f seconds ago)",
						local_airport.airport.ident,
						last_contact_seconds,
					)

				elif last_contact_seconds > WAIT_FOR_RESET_ENROUTE:
					logging.debug(
						"Aircraft is on the ground by now (last contact %.1f seconds ago)",
						last_contact_seconds,
					)
					current_status = "on_ground"

		elif snapshot_result.status == SnapshotStatus.ONLINE:
			# Aircraft is online.
			plane_state.last_contact = datetime_now()
			snapshot = snapshot_result.snapshot
			if snapshot is None:
				# Snapshot data was invalid or incomplete; skip this iteration.
				pass
			else:
				plane_state.last_registration = snapshot.registration
				plane_state.last_icao = snapshot.icao
				plane_state.last_altitude = snapshot.altitude
				plane_state.last_altitude_agl = snapshot.altitude_agl
				plane_state.last_groundspeed = snapshot.groundspeed
				plane_state.last_lat = snapshot.lat
				plane_state.last_lon = snapshot.lon
				if snapshot.runway_airport.status != AirportSearchStatus.SKIPPED:
					plane_state.last_runway_airport = snapshot.runway_airport
				if snapshot.glideslope_airport.status != AirportSearchStatus.SKIPPED:
					plane_state.last_glideslope_airport = snapshot.glideslope_airport
				if snapshot.local_airport.status == AirportSearchStatus.FOUND:
					local_airport = snapshot.local_airport
				is_airborne_flag = is_airborne(
					snapshot.altitude,
					snapshot.groundspeed,
					local_airport is not None
				)
				if is_airborne_flag is True:
					current_status = "airborne"
				elif is_airborne_flag is False:
					current_status = "on_ground"
				else:
					# Airborne status could not be determined.
					pass
		else:
			logging.error("Unknown snapshot status %s; skipping", snapshot_result.status)

		if plane_state.last_status != current_status:
			logging.debug("Status changed from %s to %s", plane_state.last_status, current_status)
			transition = get_transition(
				current_status=current_status,
				is_near_airport=local_airport is not None,
			)
			emit_transition_event(
				registration=plane_state.last_registration,
				airport=local_airport.airport if local_airport is not None else None,
				icao=plane_state.last_icao,
				transition=transition,
			)
			# Update the status.
			plane_state.last_status = current_status

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
		if _replay_log is not None:
			# In replay mode, the log processor is doing the sleeping.
			continue
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
		logging.warning("RAPIDAPI_KEY not set; cannot fetch aircraft data")
	_rapidapi_host = "adsbexchange-com1.p.rapidapi.com"
	_rapidapi_base_url = "https://adsbexchange-com1.p.rapidapi.com/v2"

	global _discord_webhook_url
	_discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
	if not _discord_webhook_url:
		logging.warning("DISCORD_WEBHOOK_URL not set; Discord notifications disabled")

	load_airports(AIRPORTS_CSV_LOCAL_PATH)
	load_runways(RUNWAYS_CSV_LOCAL_PATH)

	logging.info("Starting tracker for registration %s", registration)

	if len(sys.argv) >= 3 and sys.argv[2].strip().lower() == "--suppress-first-event":
		global _suppress_first_event
		_suppress_first_event = True
		logging.info("The first transition event will be suppressed")

	if len(sys.argv) >= 3 and sys.argv[2].strip().lower() == "--stall-speed":
		global _aircraft_stall_speed
		try:
			_aircraft_stall_speed = float(sys.argv[3].strip())
			logging.info("Aircraft stall speed set to %.1f knots", _aircraft_stall_speed)
		except ValueError:
			logging.warning("Invalid stall speed value %r; using default %.1f knots", sys.argv[3].strip(), _aircraft_stall_speed)

	if len(sys.argv) >= 4 and sys.argv[2].strip().lower() == "--replay-log":
		global _replay_log
		_replay_log = sys.argv[3].strip()

	if _replay_log is not None:
		if not os.path.isfile(_replay_log):
			logging.error("Replay log file not found: %s", _replay_log)
			raise SystemExit(1)
		_replay_log = open(_replay_log, "r", encoding="utf-8")
		logging.info("Replaying log: %s", _replay_log.name)
	
	try:
		monitor_plane(registration)
	except KeyboardInterrupt:
		logging.info("Tracker interrupted by user; exiting")
		if _replay_log is not None:
			_replay_log.close()
		raise SystemExit(0)
	except Exception as exc:
		if _replay_log is not None:
			_replay_log.close()
		logging.exception("Unexpected error occurred: %s", exc)
		raise SystemExit(1)


if __name__ == "__main__":
	main()
