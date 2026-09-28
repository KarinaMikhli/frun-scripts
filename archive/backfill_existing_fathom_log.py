#!/usr/bin/env python3
"""One-time: split the existing fathom-extraction-log.md into v2 per-run files.

SOURCE (--log): the current single fathom-extraction-log.md (read-only).
    Sections ("## YYYY-MM-DD (extracted YYYY-MM-DD ...)") are grouped by their
    EXTRACTED date — i.e. by the run that wrote them — so each output file
    holds exactly what one run appended. A section with no "extracted" date
    is filed under its own date.

OUTPUT (--out-dir = the _v2 root), same layout as build_daily_fathom_log.py:
    daily/YYYY-MM/fathom-extraction-log-YYYY-MM-DD.md
    index/YYYY-MM/fathom-index-YYYY-MM-DD.jsonl
    Refuses to start if any file it would write already exists.

Section text is copied byte-for-byte; the only thing added is a two-line
header per file. --check verifies that concatenating the output reproduces
every section of the source.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_common import month_dir  # noqa: E402
from build_daily_fathom_log import load_recording_ids, split_sections, write_run  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--archive-root", help="Fathom Meetings folder, for recording_id lookup (read-only)")
    a = ap.parse_args(argv)

    with open(a.log, encoding="utf-8") as f:
        sections = split_sections(f.read())
    runs = {}
    for s in sections:
        runs.setdefault(s[1], []).append(s)

    clash = [d for d in runs if os.path.exists(
        os.path.join(month_dir(os.path.join(a.out_dir, "daily"), d), f"fathom-extraction-log-{d}.md"))]
    if clash:
        raise SystemExit(f"REFUSING: {len(clash)} run file(s) already exist, e.g. {clash[0]} — nothing written.")

    rec_ids = load_recording_ids(a.archive_root)
    meetings = items = no_rid = 0
    for d in sorted(runs):
        _, _, rows = write_run(a.out_dir, d, runs[d], None, rec_ids,
                               script="backfill_existing_fathom_log.py", allow_rerun=False)
        meetings += len(rows)
        items += sum(r["action_items_count"] for r in rows)
        no_rid += sum(1 for r in rows if not r["recording_id"])
    print(json.dumps({"sections": len(sections), "run_files": len(runs), "meetings": meetings,
                      "action_items": items, "meetings_without_recording_id": no_rid,
                      "first_run": min(runs), "last_run": max(runs)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
