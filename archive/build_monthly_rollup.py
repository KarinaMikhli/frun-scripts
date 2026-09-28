#!/usr/bin/env python3
"""Roll a CLOSED month's daily index files into one monthly file (write-once).

    --kind slack   index/YYYY-MM/slack-index-*.jsonl   -> monthly/slack-index-YYYY-MM.jsonl
    --kind fathom  index/YYYY-MM/fathom-index-*.jsonl  -> monthly/fathom-index-YYYY-MM.jsonl

--root is the _archive_v2 (Slack) or _v2 (Fathom) folder. The rollup is a
concatenation of that month's daily index files in date order (files are
grouped by their file date, i.e. the run date; every row keeps its own "date"
field). Every line is checked to be valid JSON, and the line count of the
result must equal the rows kept.

Slack only: a message that appears in more than one daily file (same
channel_id + ts) is kept once — the FIRST copy, i.e. the one written by the
earliest run. Daily files are write-once, so a run that re-captures already
archived messages (first v3 run, 2026-09-28: 81 thread replies) can't be fixed
at the source; the rollup is where duplicates stop. Rows without a ts (the
pre-2026-09-16 backfill) are never merged. The result reports how many rows
were dropped and which (channel_id, ts) they were.

Refuses to run when:
  * the month isn't over yet (month >= current month in ET — pass --today to
    test with a different "today"),
  * the monthly file already exists (it's never rewritten),
  * there are no daily files for that month.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_common import today_et, write_once  # noqa: E402

PREFIX = {"slack": "slack-index", "fathom": "fathom-index"}


def rollup(kind, root, month, today):
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("--month must be YYYY-MM")
    if month >= today.strftime("%Y-%m"):
        raise ValueError(f"{month} isn't closed yet (today is {today}) — not rolling it up")
    pre = PREFIX[kind]
    out = os.path.join(root, "monthly", f"{pre}-{month}.jsonl")
    if os.path.exists(out):
        raise ValueError(f"{out} already exists — monthly files are never rewritten")
    files = sorted(glob.glob(os.path.join(root, "index", month, f"{pre}-{month}-*.jsonl")))
    if not files:
        raise ValueError(f"no daily {pre} files found for {month} under {root}/index/{month}/")
    lines, seen, dropped = [], set(), []
    for p in files:
        with open(p, encoding="utf-8") as f:
            for n, ln in enumerate(f, 1):
                if not ln.strip():
                    continue
                row = json.loads(ln)  # raises on a corrupt line -> nothing written
                if kind == "slack" and row.get("ts"):
                    key = (row.get("channel_id") or row.get("channel"), row["ts"])
                    if key in seen:
                        dropped.append({"channel_id": key[0], "ts": key[1], "file": os.path.basename(p)})
                        continue
                    seen.add(key)
                lines.append(ln if ln.endswith("\n") else ln + "\n")
    write_once(out, "".join(lines))
    with open(out, encoding="utf-8") as f:
        written = sum(1 for ln in f if ln.strip())
    if written != len(lines):
        raise RuntimeError(f"read-back mismatch: wrote {len(lines)} lines, found {written}")
    return {"month": month, "daily_files": len(files), "rows": written,
            "duplicates_dropped": len(dropped), "dropped": dropped, "written": out}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kind", choices=sorted(PREFIX), required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else today_et()
    try:
        print(json.dumps(rollup(a.kind, a.root, a.month, today)))
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
