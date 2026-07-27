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

POLL_SECONDS_DEFAULT = 60
POLL_SECONDS_OFFLINE = 900
POLL_SECONDS_ON_GROUND = 10
POLL_SECONDS_LOW_ALTITUDE = 10
POLL_SECONDS_ENROUTE_ALTITUDE = 300
POLL_THRESHOLD_ALTITUDE = 10000
AIRPORTS_CSV_LOCAL_PATH = "airports.csv"
AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}

# RapidAPI
_rapidapi_key = None
_rapidapi_host = None
_rapidapi_base_url = None

# Discord
_discord_webhook_url = None
_COLOR_GREEN  = 3066993   # takeoff
_COLOR_ORANGE = 15127554  # landing
_COLOR_BLUE   = 3447003   # flight update, initial state airborne
_COLOR_GRAY   = 9807270   # initial state on ground


@dataclass
class FlightSnapshot:
	registration: str
	icao: str
	altitude: int | None
	groundspeed: float | None
	lat: float | None
	lon: float | None
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
	last_nearest_airport: str | None = None
	last_nearest_airport_code: str | None = None
	last_nearest_airport_location: str | None = None
	last_nearest_airport_elevation_ft: float | None = None
	last_nearest_airport_lat: float | None = None
	last_nearest_airport_lon: float | None = None
	last_state: str = "offline"
	last_poll_seconds: int | None = None


def build_tracking_url(icao):
	return f"https://globe.adsbexchange.com/?icao={quote(icao)}"


def format_altitude(altitude):
	return f"{int(round(altitude))}ft" if altitude is not None else "unknown"


def format_groundspeed(groundspeed):
	return f"{int(round(groundspeed))}kt" if groundspeed is not None else "unknown"


def format_airport_label(code, location):
	if not code and not location:
		return "unknown"
	if code and location:
		return f"{code} ({location})"
	return code or location


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
	airports = []

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
#			logging.warning("Skipping airport with invalid elevation_ft: %r", row)
			continue

		try:
			lat = float(row.get("latitude_deg"))
			lon = float(row.get("longitude_deg"))
		except (TypeError, ValueError):
#			logging.warning("Skipping airport with invalid latitude_deg or longitude_deg: %r", row)
			continue

		code = row.get("icao_code") or row.get("ident") or "UNKNOWN"
		code = str(code).strip().upper() if code else "UNKNOWN"

		municipality = row.get("municipality") if isinstance(row.get("municipality"), str) else ""
		country = row.get("iso_country") if isinstance(row.get("iso_country"), str) else ""
		location = ", ".join(part for part in [municipality, country] if part) or "unknown location"

		airports.append(
			{
				"code": code,
				"location": location,
				"lat": lat,
				"lon": lon,
				"elevation_ft": elevation_ft,
			}
		)

	return airports


def find_nearest_airport(lat, lon, airports):
	if lat is None or lon is None:
		return None

	closest = None
	closest_nm = None
	for airport in airports:
		distance_nm = haversine_nm(lat, lon, airport["lat"], airport["lon"])
		if closest_nm is None or distance_nm < closest_nm:
			closest = airport
			closest_nm = distance_nm

	return closest, closest_nm


def send_discord_embed(embed):
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
			logging.info("Retrying Discord webhook post in %ss", retry_delay_seconds)
			time.sleep(retry_delay_seconds)

	logging.error(
		"Discord webhook post failed after %s attempts; skipping notification",
		max_attempts
	)


def make_embed(title, color, url=None, fields=None, timestamp=None):
	embed = {"title": title, "color": color, "footer": {"text": "ADS-B Exchange"}}
	if url:
		embed["url"] = url
	if fields:
		embed["fields"] = fields
	if timestamp:
		embed["timestamp"] = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
	return embed


def notify_event(title, color, url=None, fields=None, timestamp=None):
	send_discord_embed(make_embed(title=title, color=color, url=url, fields=fields, timestamp=timestamp))


def _field(name, value):
	return {"name": name, "value": str(value), "inline": True}


def emit_transition_event(registration, event_name, tracking_url, airport_label, timestamp, detection_method="confirmed"):
	logging.info("%s %s (%s)", event_name.lower(), detection_method, tracking_url)

	if event_name not in {"TAKEOFF", "LANDING"}:
		return

	color = _COLOR_GREEN
	if event_name == "LANDING":
		color = _COLOR_BLUE

	fields = []
	if event_name == "TAKEOFF" and airport_label:
		fields.append(_field("Departing", airport_label))
	elif event_name == "LANDING" and airport_label:
		fields.append(_field("Arriving", airport_label))
	notify_event(
		title=f"\u2708\ufe0f {registration} — {event_name}",
		color=color,
		url=tracking_url,
		fields=fields or None,
		timestamp=timestamp,
	)


def set_poll_interval(state):
	poll_interval = POLL_SECONDS_DEFAULT
	if state.last_state == 'offline':
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


def should_assume_takeoff(state):
	logging.debug("Checking if should assume takeoff: alt_agl=%s, haversine_nm=%s",
				  state.last_altitude_agl,
				  haversine_nm(state.last_lat, state.last_lon, state.last_nearest_airport_lat, state.last_nearest_airport_lon)
				  )
	if state.last_altitude_agl is None or state.last_altitude_agl > 5000:
		return False
	if state.last_lat is None or state.last_lon is None or state.last_nearest_airport_lat is None or state.last_nearest_airport_lon is None:
		return False
	if not state.last_nearest_airport_code and not state.last_nearest_airport_location:
		return False
	return haversine_nm(state.last_lat, state.last_lon, state.last_nearest_airport_lat, state.last_nearest_airport_lon) <= 5.0


def should_assume_landing(state):
	logging.debug("Checking if should assume landing: alt_agl=%s, haversine_nm=%s",
				  state.last_altitude_agl,
				  haversine_nm(state.last_lat, state.last_lon, state.last_nearest_airport_lat, state.last_nearest_airport_lon)
				  )
	if state.last_altitude_agl is None or state.last_altitude_agl > 5000:
		return False
	if state.last_lat is None or state.last_lon is None or state.last_nearest_airport_lat is None or state.last_nearest_airport_lon is None:
		return False
	if not state.last_nearest_airport_code and not state.last_nearest_airport_location:
		return False
	return haversine_nm(state.last_lat, state.last_lon, state.last_nearest_airport_lat, state.last_nearest_airport_lon) <= 5.0


def process_transition(state, current_state, timestamp):
	if not state.last_state in {"offline", "on_ground", "airborne"}:
		logging.warning("Unexpected last_state value: %s", state.last_state)
		return
	if not current_state in {"offline", "on_ground", "airborne"}:
		logging.warning("Unexpected current_state value: %s", current_state)
		return
	if state.last_state == current_state:
		return

	transition = None
	detection_method = "confirmed"
	if current_state == "offline" and state.last_state == "on_ground":
		transition = "offline"
	if current_state == "offline" and state.last_state == "airborne":
		if should_assume_landing(state):
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
		if should_assume_takeoff(state):
			transition = "takeoff"
			detection_method = "assumed"
		else:
			transition = "airborne"

	if transition is None:
		logging.warning("Unexpected transition from %s to %s", state.last_state, current_state)
		return

	emit_transition_event(
		state.last_registration,
		transition.upper(),
		build_tracking_url(state.last_icao),
		state.last_nearest_airport,
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
		"tail#=%r icao=%r alt_baro=%r groundspeed=%r lat=%r lon=%r",
		aircraft.get("r"),
		aircraft.get("hex"),
		aircraft.get("alt_baro"),
		aircraft.get("gs"),
		aircraft.get("lat"),
		aircraft.get("lon"),
	)

	if isinstance(aircraft.get("r"), str) and aircraft.get("r").strip():
		aircraft_r = aircraft.get("r").strip().upper()
	else:
		logging.error("Invalid registration value %r; skipping snapshot", aircraft.get("r"))
		return True, None

	if isinstance(aircraft.get("hex"), str) and aircraft.get("hex").strip():
		icao = aircraft.get("hex").strip().upper()
	else:
		logging.error("Invalid ICAO value %r; skipping snapshot", aircraft.get("hex"))
		return True, None

	try:
		groundspeed = float(aircraft.get("gs"))
	except:
		logging.error("Invalid groundspeed value %r; skipping snapshot", aircraft.get("gs"))
		return True, None

	altitude = 0
	is_airborne = False
	if isinstance(aircraft.get("alt_baro"), str):
		if aircraft.get("alt_baro").strip().lower() != "ground":
			logging.error("invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None
	else:
		try:
			altitude = round(aircraft.get("alt_baro"))
		except:
			logging.error("invalid alt_baro value %r; skipping snapshot", aircraft.get("alt_baro"))
			return True, None
		if altitude > 100.0 and groundspeed > 35.0:
			is_airborne = True

	try:
		lat = float(aircraft.get("lat"))
		lon = float(aircraft.get("lon"))
	except:
		logging.error("Invalid lat/lon values %r/%r; skipping snapshot", aircraft.get("lat"), aircraft.get("lon"))
		return True, None

	return False, FlightSnapshot(
		registration=aircraft_r,
		icao=icao,
		altitude=altitude,
		groundspeed=groundspeed,
		lat=lat,
		lon=lon,
		is_airborne=is_airborne,
		timestamp=datetime.now(timezone.utc),
	)


def monitor_plane(registration, airports):
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

				state.last_nearest_airport = None
				state.last_altitude_agl = 0
				nearest_airport, nearest_airport_nm = find_nearest_airport(snapshot.lat, snapshot.lon, airports)
				if nearest_airport:
					state.last_nearest_airport = format_airport_label(nearest_airport["code"], nearest_airport["location"])
					state.last_nearest_airport_code = nearest_airport["code"]
					state.last_nearest_airport_location = nearest_airport["location"]
					state.last_nearest_airport_elevation_ft = nearest_airport["elevation_ft"]
					state.last_nearest_airport_lat = nearest_airport["lat"]
					state.last_nearest_airport_lon = nearest_airport["lon"]
					if snapshot.is_airborne:
						state.last_altitude_agl = snapshot.altitude - nearest_airport["elevation_ft"]
					logging.debug(
						"Nearest: %s (asl=%s, lat=%s, lon=%s), nm=%.2fnm, agl=%s",
						state.last_nearest_airport_code,
						state.last_nearest_airport_elevation_ft,
						state.last_nearest_airport_lat,
						state.last_nearest_airport_lon,
						nearest_airport_nm,
						state.last_altitude_agl,
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
		raise SystemExit("Usage: python tracker.py <N-NUMBER>")
	registration = sys.argv[1].strip().upper()
	if not registration:
		raise SystemExit("N-NUMBER argument cannot be empty")

	logging.basicConfig(
		level=logging.DEBUG,
		format="%(asctime)s %(levelname)s %(message)s",
		datefmt="%Y-%m-%d %H:%M:%S",
	)

	airports = load_airports()
	logging.info("Loaded %d airports for nearest-airport/AGL calculations", len(airports))

	global _discord_webhook_url
	_discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
	if not _discord_webhook_url:
		logging.warning("WEBHOOK_URL not set; Discord notifications disabled")

	global _rapidapi_key, _rapidapi_host, _rapidapi_base_url
	_rapidapi_key = os.getenv("RAPIDAPI_KEY")
	if not _rapidapi_key:
		logging.error("RAPIDAPI_KEY not set; cannot fetch aircraft data")
		raise SystemExit(1)
	_rapidapi_host = "adsbexchange-com1.p.rapidapi.com"
	_rapidapi_base_url = "https://adsbexchange-com1.p.rapidapi.com/v2"

	logging.info("Starting tracker for tail# %s", registration)
	monitor_plane(registration, airports)


if __name__ == "__main__":
	main()
