#!/usr/bin/env python3
"""Unit tests for the v2 write-once archive scripts.  Run:  python3 -m unittest -v tests/test_archive_v2.py"""
import datetime as dt
import glob
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import archive_common as ac  # noqa: E402
import backfill_existing_fathom_log as bff  # noqa: E402
import backfill_existing_slack_archive as bfs  # noqa: E402
import build_daily_fathom_log as fl  # noqa: E402
import build_daily_slack_archive as sl  # noqa: E402
import build_monthly_rollup as ru  # noqa: E402

ET = ac.ET


def ts_at(y, mo, d, h, mi, us=123456):
    """Real-looking Slack ts for an Eastern-time moment."""
    t = dt.datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()
    return f"{int(t)}.{us:06d}"


def live(channel, kind, ts, user="Ann Lee", text="hello there", parent=None, cid="C123"):
    return {"channel": channel, "channel_id": cid, "type": kind, "ts": ts, "parent_ts": parent,
            "user": user, "user_id": "U1", "date": "1999-01-01", "time": "00:00:00", "tz": "EDT",
            "text": text, "files": [], "reactions": None, "has_thread": None}


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read_index(path):
    return ac.read_jsonl(path)


class WriteOnce(unittest.TestCase):
    def test_refuses_existing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            ac.write_once(p, "one")
            with self.assertRaises(ac.AlreadyExists):
                ac.write_once(p, "two")
            with open(p) as f:
                self.assertEqual(f.read(), "one")
            self.assertEqual(os.listdir(d), ["a.txt"])  # no temp file left behind

    def test_free_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.md")
            self.assertEqual(ac.free_path(p), p)
            open(p, "w").close()
            self.assertTrue(ac.free_path(p).endswith("x-run2.md"))

    def test_snippet(self):
        self.assertEqual(ac.snippet("<@U1|Ann> see <https://a.com|this> *now*"), "Ann see this now")
        self.assertTrue(ac.snippet(" ".join(str(i) for i in range(20))).endswith("…"))
        self.assertEqual(len(ac.snippet(" ".join(str(i) for i in range(20))).split()), 11)  # 10 words + …


class SlackDaily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.out = os.path.join(self.d, "_archive_v2")
        p = ts_at(2026, 10, 6, 9, 15)
        self.p = p
        self.rows = [
            live("1-ask", "message", p, text="Who knows a good CRM?"),
            live("1-ask", "reply", ts_at(2026, 10, 6, 9, 20), user="Bo Kim", text="HubSpot\nsecond line", parent=p),
            live("0-frun-team", "message", ts_at(2026, 10, 5, 23, 50), user="Rae", text="late night post", cid="C9"),
        ]
        write_jsonl(os.path.join(self.d, "in", "1-ask.jsonl"), self.rows[:2])
        write_jsonl(os.path.join(self.d, "in", "0-frun-team.jsonl"), self.rows[2:])
        write_jsonl(os.path.join(self.d, "in", "5-quiet.jsonl"), [])

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, date="2026-10-06", inp=None):
        return sl.main(["--date", date, "--input", inp or os.path.join(self.d, "in"), "--out-dir", self.out])

    def test_outputs_and_shape(self):
        self.assertEqual(self.run_build(), 0)
        md = os.path.join(self.out, "daily", "2026-10", "slack-archive-2026-10-06.md")
        ix = os.path.join(self.out, "index", "2026-10", "slack-index-2026-10-06.jsonl")
        text = open(md, encoding="utf-8").read()
        self.assertIn("## #1-ask", text)
        self.assertIn("## #0-frun-team", text)
        self.assertNotIn("5-quiet", text)                       # empty channel gets no section
        self.assertLess(text.index("## #0-frun-team"), text.index("## #1-ask"))
        self.assertIn("*Thread (1 reply):*", text)              # same-run reply nests under its post
        self.assertIn("2026-10-05 23:50", text)                 # entry from an earlier day shows its date
        rows = read_index(ix)
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(list(r)[:5], ["date", "channel", "user", "permalink", "snippet"])
        reply = next(r for r in rows if r["type"] == "reply")
        self.assertIn(f"thread_ts={self.p}", reply["permalink"])
        self.assertTrue(reply["permalink"].startswith("https://fractionalsunited.slack.com/archives/C123/p"))
        self.assertEqual(reply["date"], "2026-10-06")
        self.assertEqual(next(r for r in rows if r["channel"] == "0-frun-team")["date"], "2026-10-05")

    def test_second_run_same_day_never_overwrites(self):
        self.run_build()
        md = os.path.join(self.out, "daily", "2026-10", "slack-archive-2026-10-06.md")
        before = sha(md)
        self.run_build()                                        # identical re-run: everything already archived
        self.assertEqual(sha(md), before)
        self.assertFalse(os.path.exists(md.replace(".md", "-run2.md")))
        extra = os.path.join(self.d, "extra.jsonl")             # a re-run that found one new message
        write_jsonl(extra, self.rows + [live("1-ask", "message", ts_at(2026, 10, 6, 13, 0), user="Di", text="new one")])
        self.run_build(inp=extra)
        self.assertEqual(sha(md), before)
        rows = read_index(os.path.join(self.out, "index", "2026-10", "slack-index-2026-10-06-run2.jsonl"))
        self.assertEqual([r["user"] for r in rows], ["Di"])     # only the genuinely new message

    def test_size_cap_splits_into_parts_keeping_threads_together(self):
        rows = []
        for i in range(30):
            p = ts_at(2026, 10, 6, 8, i)
            rows.append(live("1-ask", "message", p, user=f"U{i}", text="word " * 150))
            rows.append(live("1-ask", "reply", ts_at(2026, 10, 6, 9, i), user="Rep", text="answer " * 40, parent=p))
        big = os.path.join(self.d, "big.jsonl")
        write_jsonl(big, rows)
        self.assertEqual(sl.main(["--date", "2026-10-06", "--input", big, "--out-dir", self.out, "--max-kb", "8"]), 0)
        mds = sorted(glob.glob(os.path.join(self.out, "daily", "2026-10", "slack-archive-2026-10-06-part*.md")))
        ixs = sorted(glob.glob(os.path.join(self.out, "index", "2026-10", "slack-index-2026-10-06-part*.jsonl")))
        self.assertGreater(len(mds), 2)
        self.assertEqual(len(mds), len(ixs))
        for f in mds + ixs:
            self.assertLessEqual(os.path.getsize(f), 8 * 1024)
        allrows = [r for f in ixs for r in read_index(f)]
        self.assertEqual(len(allrows), 60)                      # nothing lost across parts
        for f in ixs:                                           # every reply sits in its parent's part
            rs = read_index(f)
            posts = {r["ts"] for r in rs if r["type"] == "post"}
            self.assertTrue(all(r["parent_ts"] in posts for r in rs if r["type"] == "reply"))
        self.assertIn("(part 1 of", open(mds[0], encoding="utf-8").read())

    def test_no_messages_writes_nothing(self):
        empty = os.path.join(self.d, "empty")
        write_jsonl(os.path.join(empty, "a.jsonl"), [])
        self.assertEqual(self.run_build(inp=empty), 0)
        self.assertFalse(os.path.exists(self.out))

    def test_duplicate_ts_kept_once(self):
        dup = os.path.join(self.d, "dup.jsonl")
        write_jsonl(dup, self.rows[:1] * 3)
        self.run_build(inp=dup)
        rows = read_index(os.path.join(self.out, "index", "2026-10", "slack-index-2026-10-06.jsonl"))
        self.assertEqual(len(rows), 1)

    def test_late_reply_names_parent_from_earlier_index(self):
        self.run_build()
        late = os.path.join(self.d, "late.jsonl")
        write_jsonl(late, [live("1-ask", "reply", ts_at(2026, 10, 8, 10, 0), user="Cy", text="also Pipedrive", parent=self.p)])
        self.run_build(date="2026-10-08", inp=late)
        text = open(os.path.join(self.out, "daily", "2026-10", "slack-archive-2026-10-08.md"), encoding="utf-8").read()
        self.assertIn("reply to Ann Lee's post from 2026-10-06", text)

    def test_bad_row_rejected(self):
        bad = os.path.join(self.d, "bad.jsonl")
        write_jsonl(bad, [{"text": "no channel"}])
        self.assertEqual(self.run_build(inp=bad), 2)
        self.assertFalse(os.path.exists(self.out))

    def test_date_uses_eastern_time_from_ts(self):
        r = sl.normalize(live("x", "message", ts_at(2026, 10, 6, 0, 30)), 0, None)
        self.assertEqual((r["date"], r["time"]), ("2026-10-06", "00:30"))

    def test_pacific_backfill_row_converted(self):
        row = {"channel": "x", "type": "message", "ts": None, "date": "2026-06-01", "time": "22:30", "tz": "PDT",
               "user": "Ann", "text": "hi", "source": "backfill-from-md"}
        r = sl.normalize(row, 0, None)
        self.assertEqual((r["date"], r["time"]), ("2026-06-02", "01:30"))


class SlackBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.src = os.path.join(self.d, "src")
        self.out = os.path.join(self.d, "_archive_v2")
        bf = lambda **k: {"channel": "1-ask", "channel_id": "C1", "ts": None, "parent_ts": None, "user_id": None,
                          "tz": "EDT", "files": [], "source": "backfill-from-md", **k}
        self.rows = [
            bf(type="message", date="2026-09-01", time="10:00", user="Ann", text="Post A"),
            bf(type="reply", date="2026-09-01", time="11:00", user="Bo", text="Reply to A", tz=None),
            bf(type="message", date="2026-09-02", time="09:00", user="Cy",
               text="_Late-discovered reply — thread started by Ann on 2026-09-01: “Post A”_\n\nlate words"),
            bf(type="message", date="2026-09-03", time="08:00", user="Di", text="Same words"),
            live("1-ask", "message", ts_at(2026, 9, 3, 8, 0), user="Di", text="Same words", cid="C1"),
        ]
        write_jsonl(os.path.join(self.src, "1-ask.jsonl"), self.rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backfill(self):
        m, _ = bfs.run_backfill(self.src, self.out, "2026-09-27")
        self.assertEqual(m["rows_read"], 5)
        self.assertEqual(m["rows_written"], 4)                   # Markdown copy of the live message dropped
        self.assertEqual(len(m["dropped_duplicates"]), 1)
        d1 = open(os.path.join(self.out, "daily", "2026-09", "slack-archive-2026-09-01.md"), encoding="utf-8").read()
        self.assertIn("*Thread (1 reply):*", d1)                 # old reply nests under its parent
        d2 = open(os.path.join(self.out, "daily", "2026-09", "slack-archive-2026-09-02.md"), encoding="utf-8").read()
        self.assertIn("reply to Ann's post from 2026-09-01", d2)  # old late-reply marker became a real reply
        self.assertNotIn("Late-discovered", d2)
        ix2 = read_index(os.path.join(self.out, "index", "2026-09", "slack-index-2026-09-02.jsonl"))
        self.assertEqual(ix2[0]["type"], "reply")
        ix3 = read_index(os.path.join(self.out, "index", "2026-09", "slack-index-2026-09-03.jsonl"))
        self.assertEqual(len(ix3), 1)
        self.assertIsNotNone(ix3[0]["permalink"])                # the live copy (with permalink) was kept

    def test_reply_also_sent_to_channel_kept_once_and_recorded(self):
        p = ts_at(2026, 9, 4, 9, 0)
        r = ts_at(2026, 9, 4, 9, 5)
        write_jsonl(os.path.join(self.src, "2-loc-co.jsonl"), [
            live("2-loc-co", "message", p, text="Meetup Friday"),
            live("2-loc-co", "reply", r, user="Jo", text="We are live!", parent=p),
            live("2-loc-co", "message", r, user="Jo", text="We are live!"),
        ])
        m, _ = bfs.run_backfill(self.src, self.out, "2026-09-27")
        reasons = [d["reason"] for d in m["dropped_duplicates"]]
        self.assertEqual(reasons.count("same message ID twice (reply also sent to channel)"), 1)
        self.assertEqual(m["rows_read"] - len(m["dropped_duplicates"]), m["rows_written"])

    def test_refuses_when_output_exists(self):
        bfs.run_backfill(self.src, self.out, "2026-09-27")
        before = sha(os.path.join(self.out, "daily", "2026-09", "slack-archive-2026-09-01.md"))
        with self.assertRaises(SystemExit):
            bfs.run_backfill(self.src, self.out, "2026-09-28")
        self.assertEqual(before, sha(os.path.join(self.out, "daily", "2026-09", "slack-archive-2026-09-01.md")))

    def test_catchup_only_new_rows(self):
        _, mpath = bfs.run_backfill(self.src, self.out, "2026-09-27")
        with open(os.path.join(self.src, "1-ask.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(live("1-ask", "message", ts_at(2026, 9, 28, 9, 0), user="Ed", text="new", cid="C1")) + "\n")
        res = bfs.run_catchup(self.src, self.out, mpath, "2026-09-29")
        self.assertEqual(res["messages"], 1)
        rows = read_index(os.path.join(self.out, "index", "2026-09", "slack-index-2026-09-29.jsonl"))
        self.assertEqual([r["user"] for r in rows], ["Ed"])


FATHOM_LOG = """# Fathom Extraction Log
<!-- header -->

## 2026-09-08 (extracted 2026-09-09)

---

### Meeting 1: FrUn Advisory Board Meeting
- **Date:** 2026-09-08, 1:00–2:00 PM ET (60 min)
- **Attendees:** Karina, Todd Nilson
- **Fathom link:** https://fathom.video/calls/111

**Action Items:**
- [KARINA] Email Todd Nilson the HubSpot deck
- [MEMBER — Todd] Review pricing
  - sub-bullet that is not its own item

**Key Decisions:**
- Keep pricing at $150

**Context:**
- Discussed Trova

---

### Note: Tammy 1:1 (not run)
- skipped

## 2026-09-09 (extracted 2026-09-09)

---

### Meeting 1: Weekly Check-in with Rae
- **Date:** 2026-09-09, 12:00–12:30 PM ET (30 min)
- **Fathom link:** https://fathom.video/share/abc

**Action Items:**
- None

**Discussion Points / Possible Action Items:**
- Maybe revisit onboarding

**Key Decisions:**
- None

---

## 2026-09-10 (extracted 2026-09-10 — Thursday run)

### Meeting 1: Monthly Tool Talk
- **Date:** 2026-09-10, 1:00–2:00 PM ET

**Action Items:**
- [TAMMY] Post recap in Slack
"""


class Fathom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.log = os.path.join(self.d, "fathom-extraction-log.md")
        open(self.log, "w", encoding="utf-8").write(FATHOM_LOG)
        self.arch = os.path.join(self.d, "Fathom Meetings", "2026", "09")
        os.makedirs(self.arch)
        open(os.path.join(self.arch, "2026-09-09_Weekly.md"), "w").write(
            "---\ntitle: Weekly\nfathom_url: https://fathom.video/calls/222\nshare_url: https://fathom.video/share/abc\n"
            "recording_id: 999\n---\n# body\nrecording_id: WRONG\n")
        self.out = os.path.join(self.d, "_v2")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        secs = fl.split_sections(FATHOM_LOG)
        self.assertEqual([(s[0], s[1]) for s in secs],
                         [("2026-09-08", "2026-09-09"), ("2026-09-09", "2026-09-09"), ("2026-09-10", "2026-09-10")])
        ms = fl.split_meetings(secs[0][2], secs[0][0])
        self.assertEqual([m["title"] for m in ms], ["FrUn Advisory Board Meeting"])  # Note: heading skipped
        self.assertEqual(ms[0]["action_items_count"], 2)                           # sub-bullet not counted
        self.assertEqual(ms[0]["owners"], ["KARINA", "MEMBER"])
        self.assertIn("Todd Nilson", fl.auto_keywords(ms[0]))
        rae = fl.split_meetings(secs[1][2], secs[1][0])[0]
        self.assertEqual((rae["action_items_count"], rae["discussion_points_count"]), (0, 1))  # "- None" isn't an item

    def test_daily_build_with_meta(self):
        inp = os.path.join(self.d, "today.md")
        open(inp, "w", encoding="utf-8").write(FATHOM_LOG.split("## 2026-09-10")[0].split("<!-- header -->")[1])
        meta = os.path.join(self.d, "meta.json")
        json.dump({"meetings": [{"title": "FrUn Advisory Board Meeting", "recording_id": "555",
                                 "keywords": ["pricing", "board"]}]}, open(meta, "w"))
        self.assertEqual(fl.main(["--date", "2026-09-09", "--input", inp, "--out-dir", self.out,
                                  "--meta", meta, "--archive-root", os.path.join(self.d, "Fathom Meetings")]), 0)
        rows = read_index(os.path.join(self.out, "index", "2026-09", "fathom-index-2026-09-09.jsonl"))
        self.assertEqual(list(rows[0])[:6], ["date", "meeting_title", "recording_id", "url", "action_items_count", "keywords"])
        self.assertEqual((rows[0]["recording_id"], rows[0]["keywords"]), ("555", ["pricing", "board"]))
        self.assertEqual(rows[1]["recording_id"], "999")          # looked up via share_url, frontmatter only
        self.assertEqual(rows[0]["date"], "2026-09-08")           # meeting's own date, file is the run date
        md = os.path.join(self.out, "daily", "2026-09", "fathom-extraction-log-2026-09-09.md")
        fl.main(["--date", "2026-09-09", "--input", inp, "--out-dir", self.out])
        self.assertTrue(os.path.exists(md.replace(".md", "-run2.md")))

    def test_no_section_is_an_error(self):
        inp = os.path.join(self.d, "junk.md")
        open(inp, "w").write("nothing here")
        self.assertEqual(fl.main(["--date", "2026-09-09", "--input", inp, "--out-dir", self.out]), 2)

    def test_backfill_groups_by_run_and_preserves_text(self):
        bff.main(["--log", self.log, "--out-dir", self.out])
        files = sorted(glob.glob(os.path.join(self.out, "daily", "*", "*.md")))
        self.assertEqual([os.path.basename(f) for f in files],
                         ["fathom-extraction-log-2026-09-09.md", "fathom-extraction-log-2026-09-10.md"])
        joined = ""
        for f in files:
            body = open(f, encoding="utf-8").read()
            joined += body[body.index("\n## "):].lstrip("\n")
        for _, _, sec in fl.split_sections(FATHOM_LOG):
            self.assertIn(sec.rstrip(), joined)                  # every source section copied verbatim
        with self.assertRaises(SystemExit):
            bff.main(["--log", self.log, "--out-dir", self.out])  # never overwrites


class Rollup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for day, n in (("2026-08-03", 2), ("2026-08-04", 3)):
            write_jsonl(os.path.join(self.root, "index", "2026-08", f"slack-index-{day}.jsonl"),
                        [{"date": day, "i": i} for i in range(n)])
        write_jsonl(os.path.join(self.root, "index", "2026-08", "slack-index-2026-08-04-run2.jsonl"), [{"i": 9}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_rollup(self):
        res = ru.rollup("slack", self.root, "2026-08", dt.date(2026, 9, 28))
        self.assertEqual((res["daily_files"], res["rows"]), (3, 6))
        with self.assertRaises(ValueError):
            ru.rollup("slack", self.root, "2026-08", dt.date(2026, 9, 28))   # already exists

    def test_slack_duplicates_kept_once(self):
        # same message captured by the 9/17 run and again by a later run -> one copy, the first
        d = os.path.join(self.root, "index", "2026-07")
        write_jsonl(os.path.join(d, "slack-index-2026-07-17.jsonl"),
                    [{"channel_id": "C1", "ts": "1.1", "snippet": "orig"},
                     {"channel_id": "C1", "ts": "1.2", "snippet": "b"},
                     {"channel_id": "C2", "snippet": "no ts, backfill"}])
        write_jsonl(os.path.join(d, "slack-index-2026-07-28-part1.jsonl"),
                    [{"channel_id": "C1", "ts": "1.1", "snippet": "recaptured"},
                     {"channel_id": "C2", "ts": "1.1", "snippet": "other channel, same ts"},
                     {"channel_id": "C2", "snippet": "no ts, backfill"}])
        res = ru.rollup("slack", self.root, "2026-07", dt.date(2026, 9, 28))
        self.assertEqual((res["rows"], res["duplicates_dropped"]), (5, 1))
        self.assertEqual(res["dropped"][0]["file"], "slack-index-2026-07-28-part1.jsonl")
        with open(res["written"]) as f:
            snippets = [json.loads(l)["snippet"] for l in f]
        self.assertIn("orig", snippets)
        self.assertNotIn("recaptured", snippets)

    def test_fathom_never_deduped(self):
        d = os.path.join(self.root, "index", "2026-07")
        write_jsonl(os.path.join(d, "fathom-index-2026-07-01.jsonl"), [{"ts": "1"}, {"ts": "1"}])
        res = ru.rollup("fathom", self.root, "2026-07", dt.date(2026, 9, 28))
        self.assertEqual((res["rows"], res["duplicates_dropped"]), (2, 0))

    def test_refuses_open_month(self):
        with self.assertRaises(ValueError):
            ru.rollup("slack", self.root, "2026-09", dt.date(2026, 9, 28))
        with self.assertRaises(ValueError):
            ru.rollup("slack", self.root, "2026-08", dt.date(2026, 8, 31))

    def test_corrupt_line_writes_nothing(self):
        with open(os.path.join(self.root, "index", "2026-08", "slack-index-2026-08-05.jsonl"), "w") as f:
            f.write("{not json\n")
        with self.assertRaises(json.JSONDecodeError):
            ru.rollup("slack", self.root, "2026-08", dt.date(2026, 9, 28))
        self.assertFalse(os.path.exists(os.path.join(self.root, "monthly")) and
                         os.listdir(os.path.join(self.root, "monthly")))

    def test_no_files(self):
        with self.assertRaises(ValueError):
            ru.rollup("fathom", self.root, "2026-08", dt.date(2026, 9, 28))


if __name__ == "__main__":
    unittest.main()
