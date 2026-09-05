"""
Unit tests for gcor_chat_session_ingest.py's pure logic — no live stack
needed. Covers the two real bugs found and fixed on 2026-08-27
(docs/adr/0010's addendum): _flatten_messages reading the wrong field, and
a race in the debounce/in-flight-archive guard.

Run inside a container that has pydantic + httpx (e.g. openwebui or proxy):
    docker exec eedgeai-openwebui-1 python3 -m unittest discover \
        -s /tmp/eedgeai-tests -p 'test_gcor_*.py' -v
(copy this repo's openwebui-functions/ in first, e.g. via docker cp — the
Function file under test is loaded directly by path below, not installed.)

Or from a host Python environment with `pip install pydantic httpx`:
    python3 -m unittest openwebui-functions/tests/test_gcor_chat_session_ingest.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "gcor_chat_session_ingest.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("gcor_chat_session_ingest_under_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestFlattenMessages(unittest.TestCase):
    """Regression coverage for the exact bug found 2026-08-27: chat.messages
    (a flat array) is NOT reliably kept in sync with the real conversation —
    observed directly against a real chat where it held only 1 item after a
    genuine 6-message exchange. chat.history.messages (an id-keyed dict) is
    authoritative and must be preferred."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_prefers_history_messages_over_stale_flat_array(self):
        chat = {
            # Deliberately stale/incomplete, as observed in production.
            "messages": [{"role": "user", "content": "only the latest turn"}],
            "history": {
                "messages": {
                    "m1": {"role": "user", "content": "first", "timestamp": 1},
                    "m2": {"role": "assistant", "content": "second", "timestamp": 2},
                    "m3": {"role": "user", "content": "third", "timestamp": 3},
                },
            },
        }
        result = self.mod._flatten_messages(chat)
        self.assertEqual(len(result), 3, "must use the authoritative history.messages, not the stale flat array")
        self.assertEqual([m["content"] for m in result], ["first", "second", "third"])

    def test_falls_back_to_flat_array_when_history_missing(self):
        chat = {"messages": [{"role": "user", "content": "only source available"}]}
        result = self.mod._flatten_messages(chat)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "only source available")

    def test_empty_chat_returns_empty_list(self):
        self.assertEqual(self.mod._flatten_messages({}), [])

    def test_skips_malformed_entries(self):
        chat = {
            "history": {
                "messages": {
                    "m1": {"role": "user", "content": "valid"},
                    "m2": {"role": "user"},          # missing content — skipped
                    "m3": "not even a dict",           # malformed — skipped
                    "m4": {"content": "missing role"}, # missing role — skipped
                },
            },
        }
        result = self.mod._flatten_messages(chat)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "valid")


class TestDebounceAndInFlightGuard(unittest.TestCase):
    """Regression coverage for the concurrency bug found while unit-testing
    this fix before installing it: outlet()'s "supersede the pending
    debounce timer" logic could cancel a task that had already moved past
    its debounce wait and into an active archive, clearing the in-flight
    guard via its own `finally` and letting a second, genuinely concurrent
    archive start for the same chat."""

    def setUp(self):
        self.mod = _load_module()
        self.calls: list[str] = []

    def _patch_ingest(self, fn):
        self.mod.Filter._ingest_session = fn

    def test_burst_of_rapid_turns_coalesces_to_one_archive(self):
        async def fake_ingest(_self, chat_id, auth, cookie):
            self.calls.append(chat_id)
            await asyncio.sleep(0.05)
        self._patch_ingest(fake_ingest)

        async def scenario():
            f = self.mod.Filter()
            f.valves.debounce_seconds = 0.1
            for _ in range(5):
                await f.outlet({"chat_id": "chat-A"})
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
        self.assertEqual(self.calls, ["chat-A"], "a burst of rapid turns must coalesce to exactly one archive")

    def test_turn_during_in_flight_archive_is_skipped_not_stacked(self):
        async def fake_ingest(_self, chat_id, auth, cookie):
            self.calls.append("start")
            await asyncio.sleep(0.3)
            self.calls.append("end")
        self._patch_ingest(fake_ingest)

        async def scenario():
            f = self.mod.Filter()
            f.valves.debounce_seconds = 0.05
            await f.outlet({"chat_id": "chat-C"})
            await asyncio.sleep(0.1)   # now genuinely mid-archive (in_flight)
            await f.outlet({"chat_id": "chat-C"})  # must be skipped, not stacked
            await asyncio.sleep(0.4)

        asyncio.run(scenario())
        self.assertEqual(
            self.calls, ["start", "end"],
            "a turn arriving during an in-flight archive must not start a second, concurrent one",
        )

    def test_sequential_well_spaced_turns_each_archive_independently(self):
        async def fake_ingest(_self, chat_id, auth, cookie):
            self.calls.append(chat_id)
        self._patch_ingest(fake_ingest)

        async def scenario():
            f = self.mod.Filter()
            f.valves.debounce_seconds = 0.05
            await f.outlet({"chat_id": "chat-D"})
            await asyncio.sleep(0.15)  # let the first archive fully complete
            await f.outlet({"chat_id": "chat-D"})  # a later, separate turn
            await asyncio.sleep(0.15)

        asyncio.run(scenario())
        self.assertEqual(
            self.calls, ["chat-D", "chat-D"],
            "turns separated by more than the debounce window must each archive, not be permanently blocked",
        )


if __name__ == "__main__":
    unittest.main()
