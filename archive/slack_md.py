#!/usr/bin/env python3
"""Convert raw Slack MCP read outputs into a clean, searchable Markdown archive.

Modes:
  --list-threads  : print the Message TS of every parent that has a thread (from page files)
  (default build) : produce the .md file AND a structured JSONL sidecar with the same
                     kept messages. The JSONL path and channel id are auto-derived from
                     --out and manifest.json unless --jsonl-out / --channel-id override
                     them -- there is no flag required to get the sidecar. This also
                     applies to --late-replies mode.

Filtering: drops automation/system authors and join/leave/invite system lines.
Keeps only human messages. Reactions and emoji shortcodes are stripped from the
Markdown body (but reactions are preserved in the JSONL sidecar when requested).
Thread replies are nested under their parent (bot replies also dropped).
"""
import argparse, json, re, sys, os, datetime

BOT_NAMES = {"Auto Bot","Zapier","Make","Make.com","Relay","Relay.app","Coda","Coda.io",
             "Slackbot","Slack","Google Calendar","Google Drive","GitHub","Reacji Channeler"}
BOT_UIDS = {"USLACKBOT","USLACK","U066EN7E35G","U08J1DSE5GU",
            "B086GQK1G6Q"}  # B086GQK1G6Q = FrUn birthday/celebration bot (posts under "Karina Mikhli")
KEEP_BOTS = set()  # per-channel allowlist of bot names/uids to keep (e.g. Auto Bot in 0-frun-updates)

def _default_jsonl_path(out_path):
    """Derive the JSONL sidecar path from --out when --jsonl-out isn't given, so the
    sidecar is written by default with no flag required. Mirrors the directory
    convention run_backfill_all.py already uses: .../Slack/_archive/X.md ->
    .../Slack/_jsonl/archive/X.jsonl (and the same shape for _history)."""
    if not out_path:
        return ""
    out_path = os.path.abspath(out_path)
    d = os.path.dirname(out_path)
    base = os.path.splitext(os.path.basename(out_path))[0]
    parent = os.path.basename(d)
    root = os.path.dirname(d)
    sub = parent[1:] if parent.startswith("_") else parent
    return os.path.join(root, "_jsonl", sub, base + ".jsonl")


def _lookup_channel_id(name):
    """Best-effort default for --channel-id: look it up in manifest.json (next to
    this script) by channel name, when the caller didn't pass one explicitly."""
    if not name:
        return ""
    try:
        manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        for m in manifest:
            if m.get("name") == name:
                return m.get("id") or ""
    except Exception:
        pass
    return ""

SYS_PAT = re.compile(r"(has joined the channel|has left the channel|was added to the channel|"
                     r"requested to invite|joined via your invite link|has renamed the channel|"
                     r"set the channel (topic|purpose)|archived the channel|un-?archived the channel|"
                     r"pinned a message to this channel|has joined the huddle)", re.I)

def is_bot(name, uid):
    if name in KEEP_BOTS or uid in KEEP_BOTS: return False
    if uid in BOT_UIDS: return True
    if uid.startswith("B"): return True   # Slack bot/integration author IDs start with B (human uids are U/W)
    if name in BOT_NAMES: return True
    if name.endswith(".app"): return True
    if re.search(r"\bbot\b", name, re.I): return True
    return False

def load_text(path):
    raw = open(path, encoding="utf-8").read()
    s = raw.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            d = json.loads(s)
            # array wrapper: [{"type":"text","text":"<json-string or text>"}]
            if isinstance(d, list):
                for item in d:
                    if isinstance(item, dict) and item.get("text"):
                        t = item["text"]
                        try:
                            inner = json.loads(t)
                            if isinstance(inner, dict) and "messages" in inner:
                                return inner["messages"]
                        except Exception:
                            pass
                        if "=== Message from " in t or "=== Reply from " in t:
                            return t
            if isinstance(d, dict) and "messages" in d:
                return d["messages"]  # already unescaped by json
        except Exception:
            pass
    # plain text: best-effort unescape of JSON artifacts
    raw = raw.replace("\\/", "/")
    raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1),16)), raw)
    return raw

def clean(s):
    s = re.sub(r"<mailto:([^|>]+)\|[^>]+>", r"\1", s)
    s = re.sub(r"<(https?://[^|>]+)\|[^>]+>", r"\1", s)
    s = re.sub(r"<(https?://[^>]+)>", r"\1", s)
    s = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", s)
    s = re.sub(r"<@[A-Z0-9]+\|([^>]+)>", r"@\1", s)
    s = re.sub(r"<@([A-Z0-9]+)>", r"@\1", s)
    s = re.sub(r"<!(here|channel|everyone)>", r"@\1", s)
    s = s.replace("&amp;","&").replace("&lt;","<").replace("&gt;",">")
    s = re.sub(r":[a-z_][a-z0-9_+\-]*:", "", s)   # emoji shortcodes
    return s.strip()

def hhmm(t): return t[:5]

def parse_pages(files):
    msgs = {}
    for f in files:
        text = load_text(f)
        for p in text.split("=== Message from ")[1:]:
            m = re.match(r"(.*?) \(([A-Z0-9]+)\) at (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([A-Z]{3}) ===\s*\n",
                         p, re.S)
            if not m: continue
            name, uid, date, time, tz = m.groups()
            body = p[m.end():]
            ts=None; keep=[]; files_=[]; has_thread=False; reactions=None
            for ln in body.split("\n"):
                if ln.startswith("Message TS:"): ts=ln.split(":",1)[1].strip(); continue
                if ln.startswith("Reactions:"): reactions=ln.split(":",1)[1].strip(); continue
                if ln.startswith("Thread:"): has_thread=True; continue
                if ln.startswith("Files:"): files_.append(ln[len("Files:"):].strip()); continue
                keep.append(ln)
            if ts is None or ts in msgs: continue
            msgs[ts] = dict(name=name, uid=uid, date=date, time=time, tz=tz,
                            content="\n".join(keep), files=files_, has_thread=has_thread,
                            reactions=reactions)
    return msgs

def parse_threads(files):
    threads = {}
    for f in files:
        text = load_text(f)
        chunks = text.split("=== THREAD PARENT MESSAGE ===")
        for ch in chunks[1:]:
            pm = re.search(r"Message TS:\s*(\S+)", ch)
            if not pm: continue
            parent_ts = pm.group(1)
            reps = []
            after = ch.split("=== THREAD REPLIES",1)
            if len(after) < 2:
                threads[parent_ts]=reps; continue
            for r in after[1].split("--- Reply ")[1:]:
                nm = re.search(r"From:\s*(.*?) \(([A-Z0-9]+)\)\s*\nTime:\s*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([A-Z]{3})\s*\nMessage TS:\s*(\S+)\n",
                               r, re.S)
                if not nm: continue
                name,uid,date,time,tz,ts = nm.groups()
                # NOTE (fixed): previously the remainder of `r` was captured as a single
                # group and used verbatim as the reply body. That swallowed a trailing
                # "Reactions: ..." line straight into the body text (it only became
                # visible as garbled text once newlines were flattened to spaces when
                # rendering the Markdown bullet, e.g. "...I will update Reactions: +1 (1)").
                # Fixed by scanning the remainder line-by-line, same as parse_pages(),
                # so Reactions:/Files: lines are pulled out instead of leaking into body.
                rest = r[nm.end():]
                keep=[]; reactions=None; files_=[]
                for ln in rest.split("\n"):
                    if ln.startswith("Reactions:"): reactions=ln.split(":",1)[1].strip(); continue
                    if ln.startswith("Files:"): files_.append(ln[len("Files:"):].strip()); continue
                    keep.append(ln)
                body = "\n".join(keep)
                reps.append(dict(name=name,uid=uid,date=date,time=time,tz=tz,ts=ts,body=body,
                                  reactions=reactions,files=files_))
            threads[parent_ts]=reps
    return threads

def emit_late_replies(a):
    """--late-replies mode: given a freshly re-fetched raw thread dump for an
    already-archived parent (tracked in open_threads.json), append ONLY the
    replies newer than --after-ts to the archive. Never touches the original
    parent block already written to --out — this is a pure append, same as
    the normal incremental path, just for replies discovered late.

    Each late reply is rendered as its own dated block (using the reply's
    real date, so it lands in the correct 30-day scoring bucket even though
    it's appended to the end of the file, potentially out of chronological
    order relative to later sections already in the file). An italic
    cross-reference line ties it back to the original thread for anyone
    reading the raw archive.

    Also writes each kept late reply to the JSONL sidecar (a.jsonl_out), which
    main() and this function both get populated by default from --out /
    manifest.json -- see _default_jsonl_path()/_lookup_channel_id() above.
    Fixed 2026-09-16: this mode used to silently skip the JSONL sidecar.
    """
    threads = parse_threads([a.thread_file])
    reps = threads.get(a.parent_ts, [])
    after = float(a.after_ts) if a.after_ts else float(a.parent_ts)
    new_max = after
    body = []
    jsonl_rows = []
    chan_name = os.path.splitext(os.path.basename(a.out))[0] if a.out else ""
    cur_date = None
    kept = 0
    for r in sorted(reps, key=lambda r: float(r["ts"])):
        rts = float(r["ts"])
        if rts <= after:
            continue
        if rts > new_max:
            new_max = rts
        if is_bot(r["name"], r["uid"]) or SYS_PAT.search(r["body"]):
            continue
        c = clean(r["body"])
