import unittest

from tracker import (
    LOW_ALTITUDE_POLL_SECONDS,
    ENROUTE_ALTITUDE_SECONDS,
    OFFLINE_POLL_SECONDS,
    PlaneState,
    determine_airborne_state,
    classify_transition,
    should_assume_landing,
    should_enter_offline,
    determine_poll_interval,
    should_switch_poll_interval,
)


class AltBaroStateTests(unittest.TestCase):
    def test_on_ground_value_is_treated_as_grounded(self):
        altitude, is_airborne, state_name = determine_airborne_state({"alt_baro": "ground"}, "N12345")
        self.assertIsNone(altitude)
        self.assertFalse(is_airborne)
        self.assertEqual(state_name, "on_ground")

    def test_numeric_value_is_treated_as_airborne(self):
        altitude, is_airborne, state_name = determine_airborne_state({"alt_baro": 1200}, "N12345")
        self.assertEqual(altitude, 1200.0)
        self.assertTrue(is_airborne)
        self.assertEqual(state_name, "airborne")

    def test_invalid_value_is_logged_and_skipped(self):
        with self.assertLogs(level="ERROR") as captured:
            altitude, is_airborne, state_name = determine_airborne_state({"alt_baro": "unexpected"}, "N12345")
        self.assertIsNone(altitude)
        self.assertIsNone(is_airborne)
        self.assertIsNone(state_name)
        self.assertTrue(any("unexpected alt_baro value" in message for message in captured.output))

    def test_transition_classifier(self):
        self.assertEqual(classify_transition(None, "on_ground"), "online")
        self.assertEqual(classify_transition(None, "airborne"), "airborne")
        self.assertEqual(classify_transition("on_ground", "airborne"), "takeoff")
        self.assertEqual(classify_transition("airborne", "on_ground"), "landing")
        self.assertEqual(classify_transition("on_ground", None), "offline")
        self.assertIsNone(classify_transition(None, None))
        self.assertIsNone(classify_transition("airborne", "airborne"))

    def test_should_assume_landing_when_near_airport_and_low_altitude(self):
        state = PlaneState(
            last_altitude=5000,
            last_lat=40.0,
            last_lon=-74.0,
            last_nearest_airport_code="KJFK",
            last_nearest_airport_location="Queens, US",
            last_nearest_airport_lat=40.01,
            last_nearest_airport_lon=-74.01,
        )
        self.assertTrue(should_assume_landing(state))

    def test_should_not_assume_landing_when_far_from_airport(self):
        state = PlaneState(
            last_altitude=5000,
            last_lat=40.0,
            last_lon=-74.0,
            last_nearest_airport_code="KJFK",
            last_nearest_airport_location="Queens, US",
            last_nearest_airport_lat=40.0,
            last_nearest_airport_lon=-80.0,
        )
        self.assertFalse(should_assume_landing(state))

    def test_should_not_assume_landing_when_altitude_is_too_high(self):
        state = PlaneState(
            last_altitude=15000,
            last_lat=40.0,
            last_lon=-74.0,
            last_nearest_airport_code="KJFK",
            last_nearest_airport_location="Queens, US",
            last_nearest_airport_lat=40.1,
            last_nearest_airport_lon=-74.1,
        )
        self.assertFalse(should_assume_landing(state))

    def test_determine_poll_interval_uses_low_altitude_rate_near_ground(self):
        self.assertEqual(determine_poll_interval(5000, "airborne"), LOW_ALTITUDE_POLL_SECONDS)

    def test_determine_poll_interval_uses_enroute_rate_high_above_threshold(self):
        self.assertEqual(determine_poll_interval(12000, "airborne"), ENROUTE_ALTITUDE_SECONDS)

    def test_should_switch_poll_interval_uses_hysteresis(self):
        self.assertTrue(should_switch_poll_interval(9000, "airborne", LOW_ALTITUDE_POLL_SECONDS))
        self.assertFalse(should_switch_poll_interval(10000, "airborne", LOW_ALTITUDE_POLL_SECONDS))
        self.assertFalse(should_switch_poll_interval(10000, "airborne", ENROUTE_ALTITUDE_SECONDS))
        self.assertTrue(should_switch_poll_interval(11000, "airborne", ENROUTE_ALTITUDE_SECONDS))

    def test_should_enter_offline_for_startup_or_landing_scenarios(self):
        self.assertTrue(should_enter_offline(PlaneState()))
        self.assertTrue(should_enter_offline(PlaneState(last_alt_baro_state="on_ground")))

        near_airport_state = PlaneState(
            last_alt_baro_state="airborne",
            last_altitude=5000,
            last_lat=40.0,
            last_lon=-74.0,
            last_nearest_airport_code="KJFK",
            last_nearest_airport_location="Queens, US",
            last_nearest_airport_lat=40.01,
            last_nearest_airport_lon=-74.01,
        )
        self.assertTrue(should_enter_offline(near_airport_state))

        high_altitude_state = PlaneState(
            last_alt_baro_state="airborne",
            last_altitude=15000,
            last_lat=40.0,
            last_lon=-74.0,
            last_nearest_airport_code="KJFK",
            last_nearest_airport_location="Queens, US",
            last_nearest_airport_lat=40.01,
            last_nearest_airport_lon=-74.01,
        )
        self.assertFalse(should_enter_offline(high_altitude_state))

    def test_determine_poll_interval_uses_offline_rate_when_no_data(self):
        self.assertEqual(determine_poll_interval(5000, "on_ground", offline=True), OFFLINE_POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
