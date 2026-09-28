#!/usr/bin/env python3
"""Write one run's Fathom extraction log as its own write-once file (v2).

Replaces appending to the single ever-growing fathom-extraction-log.md.

INPUT (--input): a markdown file holding exactly what the run would have
    appended to the old log — one or more sections in the current Step 4
    format (checked against the live prompt + the real log, 2026-09-27):

        ## YYYY-MM-DD (extracted YYYY-MM-DD)
        ---
        ### Meeting N: <title>
        - **Date:** YYYY-MM-DD, HH:MM–HH:MM AM/PM ET (duration min)
        - **Attendees:** <names>
        - **Fathom link:** <url>
        **Action Items:**
        - [KARINA] ...
        **Discussion Points / Possible Action Items:**   (optional)
        **Key Decisions:** / **Context:**
        ---

    Optional --meta: JSON the run can pass with things only it knows —
        {"meetings": [{"title": "...", "recording_id": "...", "keywords": ["..."]}]}
    matched to meetings by title. Anything not supplied is filled in:
        recording_id  looked up from the per-meeting archive files
                      (--archive-root) by their fathom_url; null if not found
        keywords      picked automatically from the meeting's own text

OUTPUT (--out-dir = the _v2 root), both written once, never reopened:
    daily/YYYY-MM/fathom-extraction-log-YYYY-MM-DD.md
    index/YYYY-MM/fathom-index-YYYY-MM-DD.jsonl    one line per meeting:
        {"date", "meeting_title", "recording_id", "url", "action_items_count",
         "keywords", + extras: "run_date", "owners", "discussion_points_count"}

    --date is the RUN date (one run can cover meetings from two days). A
    second run on the same date gets ...-run2; nothing is ever overwritten.

Exit codes: 0 ok, 2 bad input.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from archive_common import free_path, jsonl, month_dir, write_once  # noqa: E402

SECTION_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:.*?extracted (\d{4}-\d{2}-\d{2}))?", re.M)
MEETING_RE = re.compile(r"^### (.+)$", re.M)
LABEL_RE = re.compile(r"^\*\*([^*]+):\*\*\s*$")
TAG_RE = re.compile(r"^- \[([A-Z]+)(?: — [^\]]+)?\]")
URL_RE = re.compile(r"\*\*Fathom link:\*\*\s*(\S+)")
DATE_LINE_RE = re.compile(r"\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})")

STOP = set("""
A An And Are As At Be But By For From Has Have He Her His How I If In Is It Its No Not Of On Or Our She So That The
Their Then There These They This To Was We Were What When Which Who Will With You Your Yes Also All Any Each Both
Karina Mikhli Meeting Action Items Key Decisions Context Discussion Points Possible Date Attendees Fathom Link Note
KARINA TAMMY RAE SANJA ISABELLE MEMBER TEAM ET AM PM Monday Tuesday Wednesday Thursday Friday Saturday Sunday
January February March April May June July August September October November December Discussed Agreed Plan Plans
Next Need Needs Follow Up Weekly Check Zoom Impromptu Others Other Current New One Two Three None N/A TBD
Your Our My Own Meetings Meeting Post Posts Share Repost Draft Drafts I'm Don't Fri Mon Tue Wed Thu Sat Sun
""".split())


def split_sections(text):
    """-> list of (meeting_date, extracted_date, section_text)."""
    marks = list(SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), m.group(2) or m.group(1), text[m.start():end].rstrip() + "\n"))
    return out


def split_meetings(section_text, section_date):
    heads = list(MEETING_RE.finditer(section_text))
    meetings = []
    for i, h in enumerate(heads):
        head = h.group(1).strip()
        if head.lower().startswith("note:"):
            continue  # e.g. "### Note: Tammy 1:1 (not run)" — a note, not a meeting
        end = heads[i + 1].start() if i + 1 < len(heads) else len(section_text)
        body = section_text[h.end():end]
        title = re.sub(r"^Meeting(?: \d+)?:\s*", "", head)
        d = DATE_LINE_RE.search(body)
        u = URL_RE.search(body)
        blocks = label_blocks(body)
        items = [ln for ln in blocks.get("Action Items", []) if ln.startswith("- ")]
        items = [ln for ln in items if ln.strip() not in ("- None", "- none", "- None.")]
        owners = sorted({t.group(1) for t in (TAG_RE.match(ln) for ln in items) if t})
        meetings.append(dict(
            title=title, date=d.group(1) if d else section_date, url=u.group(1).rstrip(").,") if u else None,
            action_items_count=len(items), owners=owners,
            discussion_points_count=len([ln for ln in blocks.get("Discussion Points / Possible Action Items", [])
                                         if ln.startswith("- ")]),
            body=body,
        ))
    return meetings


def label_blocks(body):
    """Group top-level lines under their **Label:** heading."""
    blocks, cur = {}, None
    for ln in body.split("\n"):
        m = LABEL_RE.match(ln.strip())
        if m:
            cur = m.group(1).strip()
            blocks[cur] = []
        elif ln.strip() in ("---",) or ln.startswith("### "):
            cur = None
        elif cur is not None:
            blocks[cur].append(ln.rstrip())
    return blocks


def auto_keywords(meeting, n=8):
    """Cheap, deterministic keywords: capitalised names/terms and acronyms,
    weighted toward the title. Good enough to answer "did X come up"."""
    text = meeting["body"]
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\*\*[^*]+:\*\*", " ", text)
    text = re.sub(r"\[[A-Z]+(?: — [^\]]+)?\]\s*", "", text)  # "- [RAE] Continue" -> "- Continue" (sentence start)
    counts = collections.Counter()
    mid_sentence = collections.Counter()  # single words seen somewhere other than a sentence start
    phrase_re = re.compile(r"(?:[A-Z][\w'&.-]*[a-z0-9][\w'&-]*|[A-Z]{2,6}\b)(?:\s+(?:[A-Z][\w'&-]+|[A-Z]{2,6}\b))*")

    def add(src, weight):
        for m in phrase_re.finditer(src):
            words = [re.sub(r"['’]s$", "", w).strip(".,;:'\"()") for w in m.group(0).split()]
            words = [w for w in words if w and w not in STOP]
            if not words:
                continue
            p = " ".join(words)
            if len(p) <= 2:
                continue
            before = src[max(0, m.start() - 2):m.start()]
            at_start = m.start() == 0 or before.endswith(("- ", ". ", ": ", "\n")) or before in ("-", "\n")
            if len(words) > 1 or not at_start:
                mid_sentence[p] += 1
            counts[p] += weight

    add(meeting["title"], 3)
    add(text, 1)
    keep, seen = [], set()
    for k, _ in counts.most_common():
        # a lone capitalised word only counts if it's capitalised mid-sentence
        # somewhere (a name/term), not just because it started a bullet
        if " " not in k and not mid_sentence[k] and not k.isupper():
            continue
        if k.lower() in seen:
            continue
        seen.add(k.lower())
        keep.append(k)
        if len(keep) == n:
            break
    return keep


def load_recording_ids(archive_root):
    """fathom_url -> recording_id, read from per-meeting archive frontmatter (read-only)."""
    ids = {}
    if not archive_root or not os.path.isdir(archive_root):
        return ids
    for p in glob.glob(os.path.join(archive_root, "*", "*", "*.md")):
        urls, rid = [], None
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                if i > 60 or (i > 0 and ln.startswith("---")):
                    break
                if ln.startswith(("fathom_url:", "share_url:")):
                    urls.append(ln.split(":", 1)[1].strip())
                elif ln.startswith("recording_id:"):
                    rid = ln.split(":", 1)[1].strip().strip('"')
        for url in urls:
            if rid:
                ids[url] = rid
    return ids


def index_rows(run_date, sections, meta, rec_ids):
    by_title = {m.get("title", "").strip().lower(): m for m in (meta or {}).get("meetings", [])}
    rows = []
    for sec_date, _, text in sections:
        for m in split_meetings(text, sec_date):
            given = by_title.get(m["title"].strip().lower(), {})
            rows.append({
                "date": m["date"], "meeting_title": m["title"],
                "recording_id": given.get("recording_id") or rec_ids.get(m["url"] or ""),
                "url": m["url"], "action_items_count": m["action_items_count"],
                "keywords": given.get("keywords") or auto_keywords(m),
                "run_date": run_date, "owners": m["owners"],
                "discussion_points_count": m["discussion_points_count"],
            })
    return rows


def render_log(run_date, sections, script):
    head = [
        f"# Fathom Extraction Log — {run_date}",
        f"<!-- Written once by {script} for the run on {run_date}; never edited afterwards. "
        "Read by daily-personal-checkin, weekly-waiting-on-checkin, friday-team-review and the "
        "Look Back/Look Forward briefs. -->",
        "",
        "",
    ]
    return "\n".join(head) + "\n".join(s[2].rstrip() + "\n" for s in sections)


def write_run(out_dir, run_date, sections, meta=None, rec_ids=None, script="build_daily_fathom_log.py", allow_rerun=True):
    md = os.path.join(month_dir(os.path.join(out_dir, "daily"), run_date), f"fathom-extraction-log-{run_date}.md")
    ix = os.path.join(month_dir(os.path.join(out_dir, "index"), run_date), f"fathom-index-{run_date}.jsonl")
    if allow_rerun:
        md, ix = free_path(md), free_path(ix)
    rows = index_rows(run_date, sections, meta, rec_ids or {})
    write_once(md, render_log(run_date, sections, script))
    write_once(ix, jsonl(rows))
    return md, ix, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="run date, YYYY-MM-DD (ET)")
    ap.add_argument("--input", required=True, help="markdown: this run's log section(s)")
    ap.add_argument("--out-dir", required=True, help="the _v2 root folder")
    ap.add_argument("--meta", help="optional JSON with recording_id/keywords per meeting title")
    ap.add_argument("--archive-root", help="Fathom Meetings folder, for recording_id lookup (read-only)")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        print("ERROR: --date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    with open(a.input, encoding="utf-8") as f:
        sections = split_sections(f.read())
    if not sections:
        print("ERROR: no '## YYYY-MM-DD' section found in --input", file=sys.stderr)
        return 2
    meta = None
    if a.meta:
        with open(a.meta, encoding="utf-8") as f:
            meta = json.load(f)
    md, ix, rows = write_run(a.out_dir, a.date, sections, meta, load_recording_ids(a.archive_root))
    print(json.dumps({"date": a.date, "meetings": len(rows),
                      "action_items": sum(r["action_items_count"] for r in rows), "written": [md, ix]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
