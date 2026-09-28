#!/usr/bin/env python3
"""Independent check that the v2 backfill lost nothing. Read-only.

Slack: every row of the old per-channel JSONL is accounted for (written, or
listed as a dropped duplicate in the manifest), per channel; every message's
text is findable in its day .md; monthly rollups equal the sum of their days.
Fathom: every section of the old log appears verbatim in exactly one run file,
and the index has one line per meeting.

Prints a report and exits 1 if any check fails.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_common import plain, read_jsonl  # noqa: E402
from build_daily_fathom_log import split_meetings, split_sections  # noqa: E402

LATE = re.compile(r"^_Late-discovered reply — .*?”_\s*", re.S)
fails = []


def check(ok, msg):
    print(("  OK   " if ok else "  FAIL ") + msg)
    if not ok:
        fails.append(msg)


def slack(source_dir, root):
    print("Slack")
    manifests = sorted(glob.glob(os.path.join(root, "_backfill", "backfill-manifest-*.json")))
    man = json.load(open(manifests[-1], encoding="utf-8"))
    src = collections.Counter()
    texts = []
    for p in sorted(glob.glob(os.path.join(source_dir, "*.jsonl"))):
        ch = os.path.basename(p)[:-6]
        rows = read_jsonl(p)[: man["source_line_counts"][os.path.basename(p)]]
        src[ch] += len(rows)
        texts += [(ch, plain(LATE.sub("", r.get("text") or ""))[:60]) for r in rows]
    dropped = collections.Counter(d["channel"] for d in man["dropped_duplicates"])

    idx = collections.Counter()
    daily_rows = collections.Counter()
    for p in glob.glob(os.path.join(root, "index", "*", "slack-index-*.jsonl")):
        rows = read_jsonl(p)
        daily_rows[os.path.basename(p)[12:19]] += len(rows)
        for r in rows:
            idx[r["channel"]] += 1
    bad = {c: (src[c], dropped[c], idx[c]) for c in src if src[c] - dropped[c] != idx[c]}
    check(not bad, f"per-channel counts: source {sum(src.values())} - duplicates {sum(dropped.values())} "
                   f"= written {sum(idx.values())} across {len(src)} channels" + (f"  MISMATCH {bad}" if bad else ""))

    md_text = collections.defaultdict(str)
    for p in glob.glob(os.path.join(root, "daily", "*", "slack-archive-*.md")):
        body = open(p, encoding="utf-8").read()
        for sec in re.split(r"\n## #", body)[1:]:
            ch, _, rest = sec.partition("\n")
            md_text[ch] += plain(rest)
    missing = [(c, t) for c, t in texts if t and t not in md_text[c]]
    check(not missing, f"message text findable in day files: {len(texts) - len(missing)}/{len(texts)}"
                       + (f"  e.g. {missing[:3]}" if missing else ""))

    for p in sorted(glob.glob(os.path.join(root, "monthly", "slack-index-*.jsonl"))):
        m = os.path.basename(p)[12:19]
        n = len(read_jsonl(p))
        check(n == daily_rows[m], f"rollup {m}: {n} rows = sum of daily files {daily_rows[m]}")

    sizes = sorted((os.path.getsize(p), os.path.basename(p)) for p in glob.glob(os.path.join(root, "daily", "*", "*.md")))
    print(f"  info largest day file: {sizes[-1][1]} ({sizes[-1][0] / 1024:.0f} KB); "
          f"median {sizes[len(sizes) // 2][0] / 1024:.0f} KB; {len(sizes)} day files")


def fathom(log, root):
    print("Fathom")
    src_secs = [s[2].rstrip() for s in split_sections(open(log, encoding="utf-8").read())]
    out = ""
    for p in sorted(glob.glob(os.path.join(root, "daily", "*", "fathom-extraction-log-*.md"))):
        out += open(p, encoding="utf-8").read()
    counts = [out.count(s) for s in src_secs]
    check(all(c == 1 for c in counts), f"log sections copied verbatim exactly once: {counts.count(1)}/{len(src_secs)}")
    n_meet = sum(len(split_meetings(s, "")) for s in src_secs)
    ix = [r for p in glob.glob(os.path.join(root, "index", "*", "fathom-index-*.jsonl")) for r in read_jsonl(p)]
    check(len(ix) == n_meet, f"index has one line per meeting: {len(ix)} = {n_meet}")
    links = len(re.findall(r"\*\*Fathom link:\*\*", "\n".join(src_secs)))
    check(sum(1 for r in ix if r["url"]) == links, f"Fathom links carried into index: {sum(1 for r in ix if r['url'])} = {links}")
    print(f"  info recording_id found for {sum(1 for r in ix if r['recording_id'])}/{len(ix)} meetings")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack-source")
    ap.add_argument("--slack-root")
    ap.add_argument("--fathom-log")
    ap.add_argument("--fathom-root")
    a = ap.parse_args()
    if a.slack_root:
        slack(a.slack_source, a.slack_root)
    if a.fathom_root:
        fathom(a.fathom_log, a.fathom_root)
    print("\nRESULT:", "ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
