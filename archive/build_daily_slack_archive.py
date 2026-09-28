#!/usr/bin/env python3
"""Build one day's write-once Slack archive files (v2).

INPUT  (--input): the JSONL rows slack_md.py already produces for this run —
    one JSON object per kept message/reply, fields as written by slack_md.py:
    channel, channel_id, type ("message"|"reply"), ts, parent_ts, user,
    user_id, date, time, tz, text, files, reactions, has_thread.
    Pass one or more files (e.g. every <channel>.jsonl the run wrote to its
    scratch folder via slack_md.py --jsonl-out). Directories are expanded to
    their *.jsonl files.

OUTPUT (--out-dir = the _archive_v2 root), both written once, never reopened:
    daily/YYYY-MM/slack-archive-YYYY-MM-DD.md   all channels, one "## #channel"
                                                section per channel that had
                                                activity; empty channels omitted
    index/YYYY-MM/slack-index-YYYY-MM-DD.jsonl  one line per message

    --date is the RUN date. The live task runs at 2:30 PM, so one run holds
    messages from yesterday afternoon through today; each entry keeps its own
    real date/time, and the index "date" field is the message's own ET date.

    If the day's file already exists (a second run the same day), this run's
    output goes to ...-run2 instead. Nothing is ever overwritten.
    If there are no messages at all, nothing is written.

    SIZE CAP (--max-kb, default 40): a day bigger than that is split into
    ...-part1, -part2 ... files (threads never split across parts), so every
    create_file the cloud task makes stays small. --max-kb 0 disables it.
    ALREADY ARCHIVED: a message whose Slack ts is already in the last 60 days
    of index files is skipped (so a re-run or replayed late reply can't
    duplicate anything); the count is reported as skipped_already_archived.
    stdout is one JSON line listing every file written and its byte size.

Exit codes: 0 ok, 2 bad input.
"""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_common import (  # noqa: E402
    et_from_label, et_from_ts, free_path, is_real_ts, jsonl, month_dir,
    permalink, read_jsonl, snippet, write_once,
)

LATE_MARKER = re.compile(
    r"^_Late-discovered reply — thread started by (?P<user>.+?) on (?P<date>\d{4}-\d{2}-\d{2}): “(?P<snip>.*?)”_\s*",
    re.S,
)


# ------------------------------------------------------------ normalizing

def normalize(row, seq, last_post_id):
    """slack_md.py JSONL row -> internal record.

    seq          running number, used as the id for rows with no real ts
    last_post_id id of the previous post in the same channel file (backfill
                 rows have no real parent ts, and the old archive always
                 wrote a thread's replies directly under their parent)
    """
    missing = [k for k in ("channel", "date", "type") if not row.get(k)]
    if missing:
        raise ValueError(f"row missing {missing}: {json.dumps(row)[:200]}")
    ts = row.get("ts")
    real = is_real_ts(ts)
    if real:
        date, time, tz = et_from_ts(ts)
    else:
        date, time = et_from_label(row["date"], row.get("time"), row.get("tz"))
        tz = row["tz"] if row.get("tz") in ("EDT", "EST") else "ET"  # converted to ET above
    text = row.get("text") or ""
    kind = "reply" if row["type"] == "reply" else "post"
    reply_to = None
    m = LATE_MARKER.match(text)
    if m:  # pre-2026-09-16 late reply, written as a standalone block in the old archive
        kind = "reply"
        reply_to = {"user": m["user"], "date": m["date"], "snippet": m["snip"]}
        text = text[m.end():].strip()
    rid = ts if real else f"bf{seq}"
    if kind == "reply":
        pid = row.get("parent_ts") if is_real_ts(row.get("parent_ts")) else (None if reply_to else last_post_id)
    else:
        pid = None
    return dict(
        id=rid, pid=pid, channel=row["channel"], channel_id=row.get("channel_id"),
        type=kind, ts=ts if real else None,
        parent_ts=row.get("parent_ts") if is_real_ts(row.get("parent_ts")) else None,
        user=row.get("user") or "(unknown)", user_id=row.get("user_id"),
        date=date, time=time, tz=tz, text=text, files=row.get("files") or [],
        reply_to=reply_to, source=row.get("source") or "live",
    )


def normalize_all(rows):
    """Normalize a list of rows in file order (order matters for backfill threads)."""
    out, last_post = [], {}
    for n, r in enumerate(rows):
        rec = normalize(r, n, last_post.get(r.get("channel")))
        if rec["type"] == "post":
            last_post[rec["channel"]] = rec["id"]
        out.append(rec)
    return out


def dedupe(recs, dropped=None):
    """Drop exact repeats of the same real message (same channel + ts).

    Happens legitimately when someone ticks "Also send to #channel" on a
    thread reply: Slack returns that one message both as a reply and as a
    post. The first copy (the reply, in slack_md.py's output order) is kept.
    Dropped copies are appended to `dropped` if given.
    """
    seen, out = set(), []
    for r in recs:
        key = (r["channel"], r["ts"]) if r["ts"] else None
        if key and key in seen:
            if dropped is not None:
                dropped.append(r)
            continue
        if key:
            seen.add(key)
        out.append(r)
    return out


# ------------------------------------------------------------ outputs

def index_row(r):
    """Spec fields first (date, channel, user, permalink, snippet), then extras
    the downstream readers need (type = post|reply is what the cooling-score
    code counts on; ts/parent_ts/user_id make rows joinable and dedupable)."""
    return {
        "date": r["date"], "channel": r["channel"], "user": r["user"],
        "permalink": permalink(r["channel_id"], r["ts"], r["parent_ts"]),
        "snippet": snippet(r["text"]),
        "time": r["time"], "type": r["type"], "ts": r["ts"], "parent_ts": r["parent_ts"],
        "user_id": r["user_id"], "channel_id": r["channel_id"], "source": r["source"],
    }


def _sort_key(r):
    return (r["date"], r["time"], r["ts"] or "", r["id"])


def _body_lines(text, indent=""):
    lines = (text or "").strip().split("\n") if (text or "").strip() else ["_(no text)_"]
    return [indent + ln if ln else "" for ln in lines]


def render_day(file_date, recs, basis, parent_lookup=None, generated=None, script="build_daily_slack_archive.py",
               part=""):
    parent_lookup = parent_lookup or {}
    posts = {r["id"]: r for r in recs if r["type"] == "post"}
    nested, loose = {}, []
    for r in recs:
        if r["type"] != "reply":
            continue
        if r["pid"] in posts and posts[r["pid"]]["channel"] == r["channel"]:
            nested.setdefault(r["pid"], []).append(r)
        else:
            loose.append(r)

    channels = sorted({r["channel"] for r in recs})
    n_posts = sum(1 for r in recs if r["type"] == "post")
    out = [
        f"# FrUn Slack archive — {file_date}{part}",
        "",
        f"> **Contents:** {n_posts} posts · {len(recs) - n_posts} replies · {len(channels)} channels  ",
        f"> **Date basis:** {basis}  ",
        f"> **Written once:** {generated or file_date} by {script} — this file is never edited afterwards",
        "",
    ]
    for ch in channels:
        out += ["", f"## #{ch}", ""]
        items = [r for r in recs if r["channel"] == ch and (r["type"] == "post" or r in loose)]
        for r in sorted(items, key=_sort_key):
            stamp = r["time"] if r["date"] == file_date else f"{r['date']} {r['time']}"
            if r["type"] == "post":
                out += [f"**{stamp} {r['tz']} — {r['user']}**  ", ""]
                out += _body_lines(r["text"])
                if r["files"]:
                    out += ["", f"*Files: {'; '.join(map(str, r['files']))}*"]
                reps = sorted(nested.get(r["id"], []), key=_sort_key)
                if reps:
                    out += ["", f"*Thread ({len(reps)} {'reply' if len(reps) == 1 else 'replies'}):*"]
                    for rp in reps:
                        rstamp = rp["time"] if rp["date"] == r["date"] else f"{rp['date']} {rp['time']}"
                        body = _body_lines(rp["text"], "    ")
                        out.append(f"  - **{rstamp} — {rp['user']}:** {body[0].strip()}")
                        out += body[1:]
            else:
                ref = r["reply_to"] or parent_lookup.get(r["pid"])
                if ref:
                    where = f"reply to {ref['user']}'s post from {ref['date']}: “{ref['snippet']}”"
                else:
                    link = permalink(r["channel_id"], r["parent_ts"]) if r["parent_ts"] else None
                    where = f"reply in an earlier thread ({link})" if link else "reply in an earlier thread"
                out += [f"**{stamp} {r['tz']} — {r['user']}** _({where})_  ", ""]
                out += _body_lines(r["text"])
            out += ["", "---"]
    return "\n".join(out).rstrip() + "\n"


def _groups(recs):
    """Units that must stay in the same file: a post with its nested replies,
    or a loose reply on its own. Returned in file order."""
    posts = {r["id"] for r in recs if r["type"] == "post"}
    by_parent, out = {}, []
    for r in recs:
        if r["type"] == "reply" and r["pid"] in posts:
            by_parent.setdefault(r["pid"], []).append(r)
    for r in recs:
        if r["type"] == "post":
            out.append([r] + by_parent.get(r["id"], []))
        elif r["pid"] not in posts:
            out.append([r])
    return out


def split_parts(file_date, recs, basis, parent_lookup, generated, kw, max_bytes):
    """Greedy-pack thread groups into parts whose .md AND index both stay
    under max_bytes (a single oversized thread still gets a part of its own)."""
    size = lambda rs: max(len(render_day(file_date, rs, basis, parent_lookup, generated, **kw).encode()),
                          len(jsonl(index_row(r) for r in rs).encode()))
    parts, cur = [], []
    for g in _groups(recs):
        if cur and size(cur + g) > max_bytes:
            parts.append(cur)
            cur = []
        cur = cur + g
    if cur:
        parts.append(cur)
    return parts


def write_day(out_dir, file_date, recs, basis, parent_lookup=None, generated=None, script=None, allow_rerun=True,
              max_bytes=None):
    """Write the day's .md + index. Returns (md_paths, index_paths).

    With max_bytes, a day too big for one file is split into -part1, -part2 ...
    (each .md and index under max_bytes), so every single write stays small —
    the Dropbox connector's create_file is unreliable for large content."""
    recs = sorted(recs, key=_sort_key)
    kw = {"script": script} if script else {}
    parts = split_parts(file_date, recs, basis, parent_lookup, generated, kw, max_bytes) if max_bytes else [recs]
    mds, ixs = [], []
    for i, prs in enumerate(parts, 1):
        sfx = f"-part{i}" if len(parts) > 1 else ""
        md = os.path.join(month_dir(os.path.join(out_dir, "daily"), file_date), f"slack-archive-{file_date}{sfx}.md")
        ix = os.path.join(month_dir(os.path.join(out_dir, "index"), file_date), f"slack-index-{file_date}{sfx}.jsonl")
        if allow_rerun:
            md, ix = free_path(md), free_path(ix)
        label = f" (part {i} of {len(parts)})" if len(parts) > 1 else ""
        write_once(md, render_day(file_date, prs, basis, parent_lookup, generated, part=label, **kw))
        write_once(ix, jsonl(index_row(r) for r in prs))
        mds.append(md)
        ixs.append(ix)
    if max_bytes is None:
        return mds[0], ixs[0]
    return mds, ixs


def lookup_from_index(index_root, near_date, days=60):
    """Read-only scan of recent index files so a late reply can name its parent."""
    import datetime as dt
    found = {}
    if not os.path.isdir(index_root):
        return found
    lo = (dt.date.fromisoformat(near_date) - dt.timedelta(days=days)).isoformat()
    for p in glob.glob(os.path.join(index_root, "*", "slack-index-*.jsonl")):
        d = os.path.basename(p)[len("slack-index-"):][:10]
        if lo <= d <= near_date:
            for row in read_jsonl(p):
                if row.get("ts"):
                    found[row["ts"]] = {"user": row["user"], "date": row["date"], "snippet": row["snippet"]}
    return found


def expand_inputs(paths):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "*.jsonl"))) if os.path.isdir(p) else [p]
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="run date, YYYY-MM-DD (ET)")
    ap.add_argument("--input", nargs="+", required=True, help="slack_md.py JSONL file(s) or folder(s) for this run")
    ap.add_argument("--out-dir", required=True, help="the _archive_v2 root folder")
    ap.add_argument("--max-kb", type=int, default=40,
                    help="split the day into parts so no single file exceeds this many KB (default 40; 0 = never split)")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        print("ERROR: --date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        rows = []
        for f in expand_inputs(a.input):
            rows += read_jsonl(f)
        recs = dedupe(normalize_all(rows))
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not recs:
        print(json.dumps({"date": a.date, "messages": 0, "written": []}))
        return 0
    lookup = lookup_from_index(os.path.join(a.out_dir, "index"), a.date)
    # Never archive the same Slack message twice: skip anything already in the
    # last 60 days of index files (makes a re-run or a replayed late reply harmless).
    already = [r for r in recs if r["ts"] and r["ts"] in lookup]
    recs = [r for r in recs if not (r["ts"] and r["ts"] in lookup)]
    if not recs:
        print(json.dumps({"date": a.date, "messages": 0, "skipped_already_archived": len(already), "written": []}))
        return 0
    lookup.update({r["id"]: {"user": r["user"], "date": r["date"], "snippet": snippet(r["text"])} for r in recs})
    basis = ("everything captured by the archive run on this date (ET); entries from an "
             "earlier day show their own date")
    mds, ixs = write_day(a.out_dir, a.date, recs, basis, lookup, max_bytes=(a.max_kb * 1024) or None)
    if isinstance(mds, str):
        mds, ixs = [mds], [ixs]
    n_posts = sum(1 for r in recs if r["type"] == "post")
    print(json.dumps({"date": a.date, "messages": len(recs), "posts": n_posts, "replies": len(recs) - n_posts,
                      "channels": len({r["channel"] for r in recs}), "skipped_already_archived": len(already),
                      "written": [p for pair in zip(mds, ixs) for p in pair],
                      "bytes": {os.path.basename(p): os.path.getsize(p) for pair in zip(mds, ixs) for p in pair}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
