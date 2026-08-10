import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("airports", ROOT / "airports.py")
airports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(airports)


def test_runway_is_not_rejected_by_glide_slope_range_when_close_to_threshold():
	airports._runways = [
		{
			"airport_ident": "KJFK",
			"length_ft": 10000,
			"width_ft": 1000,
			"surface": "ASPH",
			"lighted": 1,
			"closed": 0,
			"le_ident": "04L",
			"le_latitude_deg": 40.6400,
			"le_longitude_deg": -73.7780,
			"le_elevation_ft": 10,
			"le_heading_degT": 40.0,
			"he_ident": "22R",
			"he_latitude_deg": 40.6500,
			"he_longitude_deg": -73.7700,
			"he_elevation_ft": 10,
			"he_heading_degT": 220.0,
			"he_displaced_threshold_ft": 0,
			"le_displaced_threshold_ft": 0,
			"airport_ref": 1,
			"id": 1,
		}
	]

	result = airports.find_nearest_runway(
		lat=40.6450,
		lon=-73.7740,
		altitude=1000,
		track=40.0,
		last_lat=40.6440,
		last_lon=-73.7750,
		groundspeed=120,
	)

	assert result.status == airports.RunwaySearchStatus.FOUND
	assert result.runway is not None
	assert result.runway.end_ident == "04L"


def test_runway_is_still_found_when_closure_is_small_but_positive():
	airports._runways = [
		{
			"airport_ident": "KJFK",
			"length_ft": 10000,
			"width_ft": 1000,
			"surface": "ASPH",
			"lighted": 1,
			"closed": 0,
			"le_ident": "04L",
			"le_latitude_deg": 40.6400,
			"le_longitude_deg": -73.7780,
			"le_elevation_ft": 10,
			"le_heading_degT": 40.0,
			"he_ident": "22R",
			"he_latitude_deg": 40.6500,
			"he_longitude_deg": -73.7700,
			"he_elevation_ft": 10,
			"he_heading_degT": 220.0,
			"he_displaced_threshold_ft": 0,
			"le_displaced_threshold_ft": 0,
			"airport_ref": 1,
			"id": 1,
		}
	]

	result = airports.find_nearest_runway(
		lat=40.6401,
		lon=-73.7779,
		altitude=120,
		track=40.0,
		last_lat=40.6400,
		last_lon=-73.7780,
		groundspeed=80,
	)

	assert result.status == airports.RunwaySearchStatus.FOUND
	assert result.runway is not None
	assert result.runway.end_ident == "04L"
