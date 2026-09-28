#!/usr/bin/env python3
"""python3 -m unittest -v tests/test_activity_scan.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import activity_scan as scan  # noqa: E402

MANIFEST = [
    {"name": "1-ask", "id": "C1"},
    {"name": "1-tools", "id": "C2"},
    {"name": "5-wtf", "id": "C3"},
    {"name": "ai-demo-day", "id": "C4", "retired": True},
]
STATE = {"1-ask": "1000.000100", "1-tools": "1000.000100", "5-wtf": "1000.000100", "ai-demo-day": "1.0"}
SCAN = {"scan_after": "900.0", "force_channels": []}


def h(cid, ts, tts=None):
    return {"channel_id": cid, "ts": ts, "thread_ts": tts}


class Decide(unittest.TestCase):
    def names(self, out):
        return [c["name"] for c in out["read_channels"]]

    def test_quiet_channels_skipped_new_post_read(self):
        out = scan.decide([h("C1", "1100.000001")], MANIFEST, STATE, SCAN)
        self.assertEqual(self.names(out), ["1-ask"])
        self.assertEqual(out["channels_skipped_quiet"], 2)
        self.assertEqual(out["late_threads"], [])

    def test_late_reply_to_archived_thread(self):
        # reply at 1200 to a parent (900) that was archived long ago -> thread recheck, channel NOT read
        out = scan.decide([h("C2", "1200.000001", "900.000001")], MANIFEST, STATE, SCAN)
        self.assertEqual(self.names(out), [])
        self.assertEqual(out["late_threads"], [{"channel": "1-tools", "id": "C2", "thread_ts": "900.000001",
                                                "after_ts": "1000.000100"}])

    def test_late_reply_floor_is_later_of_watermark_and_search_start(self):
        # a reply to this old thread was already saved yesterday; today's search started at 1150,
        # so only replies after 1150 are collected — yesterday's is never saved twice
        out = scan.decide([h("C2", "1200.000001", "900.000001")], MANIFEST, STATE, SCAN, search_after="1150")
        self.assertEqual(out["late_threads"][0]["after_ts"], "1150.000000")

    def test_reply_to_new_parent_reads_channel_only(self):
        hits = [h("C1", "1100.000001", "1100.000001"), h("C1", "1150.000001", "1100.000001")]
        out = scan.decide(hits, MANIFEST, STATE, SCAN)
        self.assertEqual(self.names(out), ["1-ask"])
        self.assertEqual(out["late_threads"], [])

    def test_already_archived_hits_ignored(self):
        out = scan.decide([h("C1", "999.000001"), h("C1", "1000.000100")], MANIFEST, STATE, SCAN)
        self.assertEqual(self.names(out), [])

    def test_outside_manifest_and_retired_ignored(self):
        out = scan.decide([h("CZZ", "2000.0"), h("C4", "2000.0")], MANIFEST, STATE, SCAN)
        self.assertEqual(self.names(out), [])
        self.assertEqual(out["ignored_hits_outside_manifest"], 2)

    def test_forced_channel_read_even_if_quiet(self):
        out = scan.decide([], MANIFEST, STATE, {"scan_after": "900.0", "force_channels": ["5-wtf"]})
        self.assertEqual(out["read_channels"], [{"name": "5-wtf", "id": "C3", "reason": "forced (failed last run)"}])

    def test_first_run_and_weekly_read_everything(self):
        self.assertEqual(len(scan.decide([], MANIFEST, STATE, None)["read_channels"]), 3)       # retired excluded
        out = scan.decide([], MANIFEST, STATE, SCAN, full=True)
        self.assertEqual({c["reason"] for c in out["read_channels"]}, {"weekly full read"})

    def test_same_thread_many_replies_listed_once(self):
        hits = [h("C2", f"12{i}0.000001", "900.000001") for i in range(5)]
        self.assertEqual(len(scan.decide(hits, MANIFEST, STATE, SCAN)["late_threads"]), 1)


if __name__ == "__main__":
    unittest.main()
