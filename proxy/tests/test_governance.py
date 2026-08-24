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


if __name__ == "__main__":
    unittest.main()
