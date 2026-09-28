#!/usr/bin/env python3
"""Decide which Slack channels and threads the daily archive run must read,
from ONE workspace-wide Slack search instead of reading every channel and
re-checking every open thread.

INPUT
  --hits      JSONL, one line per search result the run recorded:
                {"channel_id": "C…", "ts": "1790559387.738409", "thread_ts": "1790285182.514419" | null}
              thread_ts = the thread_ts in the result's permalink (null if none).
  --manifest  manifest.json (the approved channel list)
  --state     archive_state.json ({channel_name: last archived ts})
  --scan      activity_scan.json ({"scan_after": "<unix ts>", "force_channels": [...]})
              — missing file = first run: read everything.
  --full      weekly safety-net run: read every channel regardless.
  --search-after  the `after` unix ts the search used; late-reply after_ts is
              max(channel watermark, this), so a reply already saved by an earlier
              run is never collected again.

OUTPUT (stdout, one JSON object)
  read_channels   [{name, id}]  channels to read with slack_read_channel today
  late_threads    [{channel, id, thread_ts, after_ts}]  old threads that got new
                  replies → slack_read_thread + slack_md.py --late-replies
  ignored         counts of hits outside the approved list (bot/admin channels)
  reason          per read channel: "new messages" | "forced (failed last run)" |
                  "first run" | "weekly full read"

A hit is a LATE REPLY when it is a reply (thread_ts set and != ts) whose
parent (thread_ts) is at or before that channel's archive watermark — the
parent is already archived, so the normal channel read won't see the reply.
Replies to parents newer than the watermark are picked up by the normal
channel read (the channel is marked active by the parent itself).
Hits at or below the channel's watermark are ignored (already archived).
"""
import argparse
import json
import sys


def decide(hits, manifest, state, scan, full=False, search_after=None):
    channels = [c for c in manifest if not c.get("retired")]
    by_id = {c["id"]: c for c in channels}
    reasons = {}

    if full or scan is None:
        for c in channels:
            reasons[c["name"]] = "weekly full read" if full else "first run"
    else:
        for name in scan.get("force_channels", []):
            if any(c["name"] == name for c in channels):
                reasons[name] = "forced (failed last run)"

    late = {}
    ignored = 0
    for h in hits:
        c = by_id.get(h.get("channel_id"))
        if c is None:
            ignored += 1
            continue
        mark = float(state.get(c["name"]) or 0)
        ts = float(h["ts"])
        if ts <= mark:
            continue  # already archived
        tts = h.get("thread_ts")
        if tts and tts != h["ts"] and float(tts) <= mark:
            key = (c["name"], tts)
            if key not in late:
                # only replies newer than BOTH the channel watermark and the start of this
                # search window: anything older was already picked up by an earlier run's
                # scan, so it can never be saved twice
                floor = max(mark, float(search_after or 0))
                late[key] = {"channel": c["name"], "id": c["id"], "thread_ts": tts,
                             "after_ts": f"{floor:.6f}"}
            continue
        reasons.setdefault(c["name"], "new messages")

    read = [{"name": c["name"], "id": c["id"], "reason": reasons[c["name"]]}
            for c in channels if c["name"] in reasons]
    return {"read_channels": read,
            "late_threads": sorted(late.values(), key=lambda x: (x["channel"], x["thread_ts"])),
            "ignored_hits_outside_manifest": ignored,
            "channels_skipped_quiet": len(channels) - len(read)}


def load_hits(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            h = json.loads(line)
            if not h.get("channel_id") or not h.get("ts"):
                raise ValueError(f"{path}:{n}: hit needs channel_id and ts")
            out.append(h)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hits", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--scan", help="activity_scan.json (omit or missing = first run)")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--search-after", required=True,
                    help="the unix ts the search started from (its `after` value) — floor for late-reply --after-ts")
    a = ap.parse_args(argv)
    manifest = json.load(open(a.manifest, encoding="utf-8"))
    state = json.load(open(a.state, encoding="utf-8"))
    scan = None
    if a.scan:
        try:
            scan = json.load(open(a.scan, encoding="utf-8"))
        except FileNotFoundError:
            scan = None
    try:
        hits = load_hits(a.hits)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(decide(hits, manifest, state, scan, a.full, a.search_after), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
