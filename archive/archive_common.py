#!/usr/bin/env python3
"""Shared helpers for the write-once Slack + Fathom archive (v2).

Design rule for everything in this folder: every output file is written ONCE
and never reopened. write_once() refuses to touch a path that already exists,
so no script here can ever overwrite or append to an earlier day or month.
"""
import datetime as _dt
import json
import os
import re
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SLACK_WORKSPACE = "fractionalsunited"  # confirmed from permalinks already in the archive

# Slack tz labels seen in the existing data -> offset in hours from ET.
# The Slack connector reports times in the viewer's zone, and a few old rows
# were captured while that zone was Pacific.
_TZ_TO_ET_HOURS = {"EDT": 0, "EST": 0, "ET": 0, "PDT": 3, "PST": 3, "CDT": 1, "CST": 1, "MDT": 2, "MST": 2}


class AlreadyExists(Exception):
    pass


def write_once(path, content):
    """Create `path` with `content`. Refuses if the file already exists.

    Writes to a temp file first, then hard-links it into place, so a crash
    mid-write never leaves a half-written file at the real path.
    """
    if os.path.exists(path):
        raise AlreadyExists(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        os.link(tmp, path)  # fails if path appeared in the meantime -> never overwrites
    except FileExistsError:
        raise AlreadyExists(path)
    finally:
        os.remove(tmp)
    return path


def free_path(path):
    """First of path, path-run2, path-run3 ... that doesn't exist yet.

    Used when the same day legitimately gets a second run (e.g. a manual
    re-run): the second run gets its own file instead of reopening the first.
    """
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base}-run{n}{ext}"):
        n += 1
    return f"{base}-run{n}{ext}"


def jsonl(rows):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: bad JSON line ({e})")
    return rows


def month_dir(root, date_str):
    """<root>/YYYY-MM/ — keeps any one folder to ~31 files."""
    return os.path.join(root, date_str[:7])


def today_et():
    return _dt.datetime.now(ET).date()


# ---------------------------------------------------------------- Slack bits

def is_real_ts(ts):
    return isinstance(ts, str) and re.fullmatch(r"\d{9,11}\.\d{6}", ts) is not None


def et_from_ts(ts):
    """Real Slack ts -> (YYYY-MM-DD, HH:MM, tz label) in Eastern Time."""
    t = _dt.datetime.fromtimestamp(float(ts), tz=ET)
    return t.strftime("%Y-%m-%d"), t.strftime("%H:%M"), t.tzname()


def et_from_label(date, time, tz):
    """Row with only date + HH:MM + tz label (backfilled rows) -> ET.

    Rows with no tz label are assumed to already be ET (the archive's zone).
    """
    hours = _TZ_TO_ET_HOURS.get(tz or "", 0)
    if not hours:
        return date, (time or "")[:5]
    t = _dt.datetime.strptime(f"{date} {(time or '00:00')[:5]}", "%Y-%m-%d %H:%M") + _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%d"), t.strftime("%H:%M")


def permalink(channel_id, ts, parent_ts=None):
    """Slack permalink. Only possible when we have the real message ts."""
    if not channel_id or not is_real_ts(ts):
        return None
    url = f"https://{SLACK_WORKSPACE}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"
    if is_real_ts(parent_ts) and parent_ts != ts:
        url += f"?thread_ts={parent_ts}&cid={channel_id}"
    return url


_MENTION = re.compile(r"<[@#!][^>|]*\|([^>]+)>|<([@#!][^>]*)>")
_LINK = re.compile(r"<(https?://[^>|]+)\|([^>]+)>|<(https?://[^>]+)>")


def plain(text):
    """Strip Slack/markdown markup so snippets read as plain words."""
    s = text or ""
    s = _LINK.sub(lambda m: m.group(2) or m.group(3) or m.group(1), s)
    s = _MENTION.sub(lambda m: m.group(1) or m.group(2), s)
    s = re.sub(r"[*_~`>]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def snippet(text, words=10):
    w = plain(text).split(" ")
    if w == [""]:
        return ""
    out = " ".join(w[:words])
    return out + (" …" if len(w) > words else "")
