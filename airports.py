import csv
import io
import logging
import math
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

AIRPORT_TYPES = {"large_airport", "medium_airport", "small_airport"}

# If an aircraft is within this distance and altitude of an airport,
# we consider it "near" that airport.
NEAR_AIRPORT_NM_THRESHOLD = 5.0
NEAR_AIRPORT_AGL_THRESHOLD = 2000


@dataclass(frozen=True)
class AirportInfo:
	ident: str
	name: str | None
	location: str
	lat: float
	lon: float
	elevation_ft: int


class AirportSearchStatus(str, Enum):
	SKIPPED = "skipped"
	NOT_FOUND = "not_found"
	FOUND = "found"


@dataclass(frozen=True)
class AirportSearchResult:
	status: AirportSearchStatus
	airport: AirportInfo | None = None
	distance_nm: float | None = None
	altitude_agl: int | None = None
	timeout_seconds: int | None = None
	timestamp: datetime | None = None


_airports = []
_runways = []


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
			AirportInfo(
				ident=ident,
				name=row.get("name"),
				location=location,
				lat=lat,
				lon=lon,
				elevation_ft=elevation_ft,
			)
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

	airport_idents = {airport.ident for airport in _airports}

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


def bearing_deg(lat1, lon1, lat2, lon2):
	# Initial bearing from point 1 to point 2 in degrees true.
	phi1 = math.radians(lat1)
	phi2 = math.radians(lat2)
	dlambda = math.radians(lon2 - lon1)
	y = math.sin(dlambda) * math.cos(phi2)
	x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
	return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point_nm(lat, lon, bearing_deg_true, distance_nm):
	# Great-circle destination point from start, true bearing, and distance (nm).
	r_nm = 3440.065
	phi1 = math.radians(lat)
	lam1 = math.radians(lon)
	theta = math.radians(bearing_deg_true)
	delta = distance_nm / r_nm

	phi2 = math.asin(
		math.sin(phi1) * math.cos(delta)
		+ math.cos(phi1) * math.sin(delta) * math.cos(theta)
	)
	lam2 = lam1 + math.atan2(
		math.sin(theta) * math.sin(delta) * math.cos(phi1),
		math.cos(delta) - math.sin(phi1) * math.sin(phi2),
	)

	lat2 = math.degrees(phi2)
	lon2 = ((math.degrees(lam2) + 540.0) % 360.0) - 180.0
	return lat2, lon2


def is_on_extended_centerline(
	lat,
	lon,
	track,
	threshold_lat,
	threshold_lon,
	runway_heading_degt,
	xt_max_nm=0.5,
	track_tol_deg=5.0,
):
	if (
		lat is None
		or lon is None
		or track is None
		or threshold_lat is None
		or threshold_lon is None
		or runway_heading_degt is None
	):
		return False, None, None

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

	is_match = cross_track_nm <= xt_max_nm and track_error_deg <= track_tol_deg

	return is_match, cross_track_nm, track_error_deg


def find_glideslope_airport(lat, lon, track, altitude, descent_rate):
	if (
		lat is None
		or lon is None
		or track is None
		or not isinstance(altitude, int)
		or descent_rate is None
	):
		return AirportSearchResult(status=AirportSearchStatus.SKIPPED)

	if descent_rate <= 0:
		return AirportSearchResult(status=AirportSearchStatus.NOT_FOUND)

	closest = None
	closest_nm = None
	closest_altitude_agl = None

	for airport in _airports:
		distance_nm = haversine_nm(lat, lon, airport.lat, airport.lon)
		if distance_nm > 25.0:
			continue
		altitude_agl = max(altitude - airport.elevation_ft, 0)
		if altitude_agl > 10000:
			continue

		if distance_nm < 5.0 and altitude_agl < 1500:
			# Inside this range, glide angle is less reliable so we just accept the airport as a match.
			pass
		else:
			# Estimate touchdown point assuming 3 degree glide slope.
			glide_angle_deg = 3.0
			touchdown_distance_nm = altitude_agl / (math.tan(math.radians(glide_angle_deg)) * 6076.12)
			touchdown_lat, touchdown_lon = destination_point_nm(lat, lon, track, touchdown_distance_nm)
			touchdown_to_airport_nm = haversine_nm(touchdown_lat, touchdown_lon, airport.lat, airport.lon)
			if touchdown_to_airport_nm > 5.0:
				continue

		if closest_nm is None or distance_nm < closest_nm:
			closest = airport
			closest_nm = distance_nm
			closest_altitude_agl = altitude_agl

	if (
		closest is None
		or closest_nm is None
		or closest_altitude_agl is None
	):
		return AirportSearchResult(status=AirportSearchStatus.NOT_FOUND)

	return AirportSearchResult(
		status=AirportSearchStatus.FOUND,
		airport=closest,
		distance_nm=closest_nm,
		altitude_agl=closest_altitude_agl,
	)


def find_runway_airport(lat, lon, altitude, track, descent_rate):
	if (
		lat is None
		or lon is None
		or altitude is None
		or track is None
		or descent_rate is None
	):
		return AirportSearchResult(status=AirportSearchStatus.SKIPPED)

	# Leave a little buffer, aircraft may level or climb slightly while waiting
	# to intercept the glideslope.
	if descent_rate <= -200:
		return AirportSearchResult(status=AirportSearchStatus.NOT_FOUND)

	closest = None
	closest_nm = None
	closest_end_ident = None
	closest_end_elevation_ft = None

	for runway in _runways:
		for threshold_lat, threshold_lon, runway_heading_degt in (
			(runway["le_latitude_deg"], runway["le_longitude_deg"], runway["le_heading_degT"]),
			(runway["he_latitude_deg"], runway["he_longitude_deg"], runway["he_heading_degT"]),
		):
			if (
				threshold_lat == runway["le_latitude_deg"]
				and threshold_lon == runway["le_longitude_deg"]
			):
				end_ident = runway["le_ident"]
				end_elevation_ft = runway["le_elevation_ft"]
			else:
				end_ident = runway["he_ident"]
				end_elevation_ft = runway["he_elevation_ft"]

			# Compute AGL and filter out runways that are out of range.
			if isinstance(altitude, str) and altitude == "ground":
				altitude_agl = 0
			elif isinstance(altitude, (int, float)):
				altitude_agl = max(altitude - end_elevation_ft, 0)
			else:
				continue
			if altitude_agl > 10000:
				continue

			# Filter out runways that are too far away.
			distance_nm = haversine_nm(lat, lon, threshold_lat, threshold_lon)
			if distance_nm > 25.0:
				continue

			is_match, cross_track_nm, track_error_deg = is_on_extended_centerline(
				lat=lat,
				lon=lon,
				track=track,
				threshold_lat=threshold_lat,
				threshold_lon=threshold_lon,
				runway_heading_degt=runway_heading_degt,
			)

			if not is_match:
				continue

			if closest_nm is None or distance_nm < closest_nm:
				closest = runway
				closest_nm = distance_nm
				closest_end_ident = end_ident
				closest_end_elevation_ft = end_elevation_ft

	if (
		closest is None
		or closest_nm is None
		or closest_end_ident is None
		or closest_end_elevation_ft is None
	):
		return AirportSearchResult(status=AirportSearchStatus.NOT_FOUND)

	altitude_agl = None
	if isinstance(altitude, int):
		altitude_agl = altitude - closest_end_elevation_ft

	# Find the the airport that goes with the runway.
	airport = next((a for a in _airports if a.ident == closest["airport_ident"]), None)

	return AirportSearchResult(
		status=AirportSearchStatus.FOUND,
		airport=airport,
		distance_nm=closest_nm,
		altitude_agl=altitude_agl,
	)


def find_nearest_airport(lat, lon, altitude, find_local=False):
	if lat is None or lon is None:
		return AirportSearchResult(status=AirportSearchStatus.SKIPPED)

	closest = None
	closest_nm = None

	for airport in _airports:
		distance_nm = haversine_nm(lat, lon, airport.lat, airport.lon)
		if closest_nm is None or distance_nm < closest_nm:
			closest = airport
			closest_nm = distance_nm

	if closest is None or closest_nm is None:
		return AirportSearchResult(status=AirportSearchStatus.NOT_FOUND)

	altitude_agl = None
	if isinstance(altitude, int):
		altitude_agl = altitude - closest.elevation_ft

	if find_local:
		is_local = False
		if closest_nm <= NEAR_AIRPORT_NM_THRESHOLD:
			if isinstance(altitude, str) and altitude == "ground":
				is_local = True
			elif altitude_agl is not None and altitude_agl <= NEAR_AIRPORT_AGL_THRESHOLD:
				is_local = True
		if not is_local:
			return AirportSearchResult(status=AirportSearchStatus.NOT_FOUND)

	return AirportSearchResult(
		status=AirportSearchStatus.FOUND,
		airport=closest,
		distance_nm=closest_nm,
		altitude_agl=altitude_agl,
	)
