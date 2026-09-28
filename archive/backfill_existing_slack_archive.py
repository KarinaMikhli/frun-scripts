#!/usr/bin/env python3
"""One-time: split the existing per-channel Slack archive into v2 day files.

SOURCE (--source-dir): Slack/_jsonl/archive/ — one <channel>.jsonl per
    channel, kept in step with the Markdown archive by slack_md.py (checked
    2026-09-27: 13,399 of 13,400 messages match the Markdown archive; the
    differences are 2 replies stored twice in 1-ask.jsonl, dropped below, and
    one reply the Markdown itself holds twice). Read-only — never modified.

OUTPUT (--out-dir = the _archive_v2 root), one pair per calendar day (ET):
    daily/YYYY-MM/slack-archive-YYYY-MM-DD.md
    index/YYYY-MM/slack-index-YYYY-MM-DD.jsonl
    _backfill/backfill-manifest-<run date>.json   what was read, for the
                                                  cutover catch-up below
    Refuses to start if any day file it would write already exists.

KNOWN LIMITS OF THE OLD DATA (flagged, not worked around):
  * Rows backfilled from Markdown on 2026-09-16 (94% of rows) have no real
    Slack ts, so no permalink and no user_id.
  * Their thread replies carry the PARENT post's date — the old Markdown
    never stored a reply's own date — so they sit on the parent's day here.
  * A few old rows were stamped in Pacific time; they're converted to ET,
    which can move a late-evening message to the next day.

CUTOVER CATCH-UP (--catchup --date D --manifest M): after the old local task
    is paused, writes every row appended to the source files since manifest M
    as ONE capture file dated D (same rules as the daily builder), so the
    days between this backfill and the first v2 run aren't lost.
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_common import month_dir, plain, read_jsonl, write_once  # noqa: E402
from build_daily_slack_archive import dedupe, normalize_all, write_day  # noqa: E402
from archive_common import snippet  # noqa: E402

BASIS = ("messages posted on this date (ET), backfilled from the pre-v2 archive; thread replies "
         "backfilled from the old Markdown sit on their parent post's day, because the old "
         "archive never recorded a reply's own date")


def load_source(source_dir):
    """-> (records, {filename: line_count})"""
    recs, counts = [], {}
    for p in sorted(glob.glob(os.path.join(source_dir, "*.jsonl"))):
        rows = read_jsonl(p)
        with open(p, encoding="utf-8") as f:
            counts[os.path.basename(p)] = sum(1 for _ in f)
        recs += normalize_all(rows)
    return recs, counts


def drop_cross_source_duplicates(recs):
    """A message captured live (real ts) AND reconstructed from Markdown shows up
    twice. Keep the live copy; drop the Markdown copy if same channel, same
    author, same text, dated within 2 days of each other."""
    live = {}
    for r in recs:
        if r["ts"]:
            live.setdefault((r["channel"], r["user"], plain(r["text"])[:200]), []).append(r["date"])
    kept, dropped = [], []
    for r in recs:
        if not r["ts"]:
            dates = live.get((r["channel"], r["user"], plain(r["text"])[:200]), [])
            if plain(r["text"]) and any(abs((dt.date.fromisoformat(d) - dt.date.fromisoformat(r["date"])).days) <= 2 for d in dates):
                dropped.append(r)
                continue
        kept.append(r)
    return kept, dropped


def build_lookup(recs):
    return {r["id"]: {"user": r["user"], "date": r["date"], "snippet": snippet(r["text"])}
            for r in recs if r["type"] == "post"}


def run_backfill(source_dir, out_dir, run_date):
    recs, counts = load_source(source_dir)
    n_read = len(recs)
    same_id = []
    recs = dedupe(recs, same_id)
    recs, dropped = drop_cross_source_duplicates(recs)
    days = {}
    for r in recs:
        days.setdefault(r["date"], []).append(r)

    targets = [os.path.join(month_dir(os.path.join(out_dir, "daily"), d), f"slack-archive-{d}.md") for d in days]
    clash = [t for t in targets if os.path.exists(t)]
    if clash:
        raise SystemExit(f"REFUSING: {len(clash)} day file(s) already exist, e.g. {clash[0]} — nothing written.")

    lookup = build_lookup(recs)
    written = 0
    for d in sorted(days):
        write_day(out_dir, d, days[d], BASIS, lookup, generated=run_date,
                  script="backfill_existing_slack_archive.py", allow_rerun=False)
        written += 1

    manifest = {
        "run_date": run_date, "source_dir": source_dir, "source_line_counts": counts,
        "rows_read": n_read, "rows_written": len(recs),
        "dropped_duplicates": [{"channel": r["channel"], "date": r["date"], "user": r["user"],
                                "snippet": snippet(r["text"]), "reason": why}
                               for why, group in (
                                   ("same message ID twice (reply also sent to channel)", same_id),
                                   ("Markdown-reconstructed copy of a message also captured live", dropped))
                               for r in group],
        "first_day": min(days), "last_day": max(days), "day_files": written,
    }
    mpath = os.path.join(out_dir, "_backfill", f"backfill-manifest-{run_date}.json")
    write_once(mpath, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest, mpath


def run_catchup(source_dir, out_dir, manifest_path, date):
    with open(manifest_path, encoding="utf-8") as f:
        done = json.load(f)["source_line_counts"]
    new_rows = []
    for p in sorted(glob.glob(os.path.join(source_dir, "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            lines = f.read().splitlines()
        new_rows += [json.loads(ln) for ln in lines[done.get(os.path.basename(p), 0):] if ln.strip()]
    recs = dedupe(normalize_all(new_rows))
    if not recs:
        return {"date": date, "messages": 0, "written": []}
    basis = "cutover catch-up: everything the old archive captured after the backfill; entries show their own date"
    md, ix = write_day(out_dir, date, recs, basis, build_lookup(recs), script="backfill_existing_slack_archive.py --catchup")
    return {"date": date, "messages": len(recs), "written": [md, ix]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-dir", required=True, help="Slack/_jsonl/archive (read-only)")
    ap.add_argument("--out-dir", required=True, help="the _archive_v2 root folder")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="run date (manifest name / catch-up file date)")
    ap.add_argument("--catchup", action="store_true")
    ap.add_argument("--manifest", help="backfill manifest to resume from (with --catchup)")
    a = ap.parse_args(argv)
    if a.catchup:
        if not a.manifest:
            ap.error("--catchup needs --manifest")
        print(json.dumps(run_catchup(a.source_dir, a.out_dir, a.manifest, a.date)))
    else:
        m, path = run_backfill(a.source_dir, a.out_dir, a.date)
        print(json.dumps({k: m[k] for k in ("rows_read", "rows_written", "first_day", "last_day", "day_files")}
                         | {"dropped_duplicates": len(m["dropped_duplicates"]), "manifest": path}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
