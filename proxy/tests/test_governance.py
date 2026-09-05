import pathlib
import sys
import unittest


PROXY_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import governance  # noqa: E402
import main  # noqa: E402


class ValidateAccessLevelTests(unittest.TestCase):
    def test_accepts_public_and_restricted(self):
        self.assertEqual(governance.validate_access_level("public"), "public")
        self.assertEqual(governance.validate_access_level("restricted"), "restricted")

    def test_blank_or_none_defaults_to_public(self):
        self.assertEqual(governance.validate_access_level(None), "public")
        self.assertEqual(governance.validate_access_level(""), "public")
        self.assertEqual(governance.validate_access_level("   "), "public")

    def test_trims_whitespace(self):
        self.assertEqual(governance.validate_access_level("  restricted  "), "restricted")

    def test_accepts_agent_scoped_value(self):
        self.assertEqual(governance.validate_access_level("agent:buddy-1"), "agent:buddy-1")

    def test_rejects_agent_prefix_with_no_id(self):
        with self.assertRaises(ValueError):
            governance.validate_access_level("agent:")

    def test_rejects_agent_id_over_length_limit(self):
        with self.assertRaises(ValueError):
            governance.validate_access_level("agent:" + "x" * 129)

    def test_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            governance.validate_access_level("pubic")  # typo, not "public"

    def test_rejects_case_variant(self):
        with self.assertRaises(ValueError):
            governance.validate_access_level("Public")


class FilterHitsAccessLevelTests(unittest.TestCase):
    """_filter_hits' access-control branch, per docs/adr/0004."""

    def _hit(self, access_level):
        return {"id": "h1", "payload": {"access_level": access_level}}

    def test_explicit_none_access_level_treated_as_public(self):
        # p.get("access_level", "public") would return None here (key present,
        # value None); p.get("access_level") or "public" must not crash on
        # .startswith() and must keep the hit.
        result = main._filter_hits([self._hit(None)])
        self.assertEqual(len(result), 1)

    def test_restricted_is_dropped(self):
        result = main._filter_hits([self._hit("restricted")])
        self.assertEqual(result, [])

    def test_public_passes_through(self):
        result = main._filter_hits([self._hit("public")])
        self.assertEqual(len(result), 1)


class FilterHitsTemporalValidityRemovedTests(unittest.TestCase):
    """Regression test for docs/adr/0019: _filter_hits() used to drop a hit
    whose valid_from/valid_to window didn't currently contain wall-clock
    "now" — meaning a real document mentioning any specific past or future
    date/period became permanently unsearchable, root-caused after two
    sessions of chasing what looked like a Graphiti/FalkorDB staleness bug
    (docs/adr/0016, 0017, 0018). That filter is gone; confidence and
    access-control filtering are unaffected."""

    def _hit(self, valid_from=None, valid_to=None):
        return {"id": "h1", "payload": {"valid_from": valid_from, "valid_to": valid_to}}

    def test_far_past_window_is_not_dropped(self):
        # e.g. a document reporting on a quarter years ago — invalid_at
        # (mapped to valid_to) long past "now" used to be read as "expired".
        result = main._filter_hits([self._hit(
            valid_from="2013-11-19T00:00:00+00:00", valid_to="2013-12-10T17:00:00+00:00",
        )])
        self.assertEqual(len(result), 1)

    def test_far_future_window_is_not_dropped(self):
        # e.g. a document projecting a future quarter — valid_at
        # (mapped to valid_from) ahead of "now" used to be read as "not yet
        # valid". This exact shape was the original ADR 0017 reproduction.
        result = main._filter_hits([self._hit(
            valid_from="2099-01-01T00:00:00+00:00", valid_to="2099-04-01T00:00:00+00:00",
        )])
        self.assertEqual(len(result), 1)

    def test_no_temporal_fields_still_passes(self):
        result = main._filter_hits([self._hit()])
        self.assertEqual(len(result), 1)

    def test_confidence_and_access_control_still_apply_alongside(self):
        # The removed filter's neighbors keep working — this isn't a case
        # of _filter_hits silently doing nothing now. -0.1 rather than a
        # small positive value: CONFIDENCE_THRESHOLD defaults to 0.0 (see
        # main.py), so a value has to be genuinely below zero to be
        # dropped under this test's actual runtime config either way.
        low_confidence = {"id": "h1", "payload": {"confidence": -0.1, "valid_to": "2013-01-01T00:00:00+00:00"}}
        restricted = {"id": "h2", "payload": {"access_level": "restricted", "valid_from": "2099-01-01T00:00:00+00:00"}}
        result = main._filter_hits([low_confidence, restricted])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
