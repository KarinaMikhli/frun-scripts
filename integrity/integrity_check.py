#!/usr/bin/env python3
"""Instruction-integrity check for Karina's cloud scheduled tasks (Dropbox edition).

Cloud port (2026-09-28) of the Mac-only scheduled-task-health-check Step 3b script.
Deterministic on purpose: flags are computed here, not by a model reading the files,
so injected text in a checked file can't talk the check out of flagging it.

The script never talks to Dropbox or the task list itself. The calling run builds a
local MIRROR folder first, then runs this script on it:

  mirror/
    prompts/<trigger_id>.md      live instructions of every ENABLED scheduled task
    prompts/_index.json          {"<trigger_id>": "<task name>", ...}
    source/<name>                watched rule files fetched from /Claude/Source/
    handoff/<relative path>      handoff/data files changed in the last 8 days (scanned only)
    repo_head.txt                short commit hash of the frun-scripts clone (optional)

Approved state lives in Dropbox and is write-once:
  baseline  = newest  integrity/approved-YYYY-MM-DD[-runN].json
  copies    = integrity/approved-copies/<slug>--<sha12>.txt   (content-addressed, never rewritten)

Modes:
  prompts-from-triggers --triggers FILE --mirror DIR
        Build mirror/prompts/ from a saved list_triggers result (JSON). Enabled tasks only.
  check  --mirror DIR --baseline FILE --copies DIR
        Print INTEGRITY lines, or one "INTEGRITY OK" line. Read-only.
  diff   --mirror DIR --baseline FILE --copies DIR KEY
        Unified diff of one watched item vs its approved copy. Read-only.
  approve --mirror DIR --baseline FILE|none --copies DIR --out FILE --copies-out DIR (KEY... | --all)
        INTERACTIVE ONLY, after Karina approves the diff in chat. Writes a NEW baseline file
        and any new approved copies locally; the live session uploads them to Dropbox.
  allow  --mirror DIR --baseline FILE --out FILE KEY:LINE
        INTERACTIVE ONLY. Allowlists one exact benign line in a handoff file.

Keys: task:<trigger_id> · source:<file name> · repo:frun-scripts
"""
import argparse, datetime, difflib, hashlib, json, os, re, sys

SCAN_BYTES_MAX = 3_000_000
SECRET_RE = re.compile(r"(key|token|secret|credential|password|\.env)", re.I)

PATTERNS = [
    ("override phrase", re.compile(r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|your|system)\b.{0,20}\b(instructions?|rules|prompts?|guidelines|directions)\b", re.I)),
    ("override phrase", re.compile(r"\b(new instructions|system override|developer mode|jailbreak|you are now|from now on you)\b", re.I)),
    ("fake authority tag", re.compile(r"<\s*/?\s*(system|admin|anthropic|assistant|instructions?|system-reminder)\b[^>]*>", re.I)),
    ("AI-directed command", re.compile(r"\b(claude|assistant|ai agent|the agent|llm|model)\b\s*[,:\-]\s*(please\s+)?(ignore|send|forward|email|delete|remove|run|execute|post|share|upload|update|change|add|approve)\b", re.I)),
    ("concealment", re.compile(r"\b(do not|don't|dont|never)\s+(tell|inform|show|mention to|alert)\s+(karina|the user|anyone)\b|\bbefore (karina|the user) (sees|notices)\b|\bwithout (telling|asking|notifying) (karina|the user)\b", re.I)),
    ("exfiltration request", re.compile(r"\b(send|forward|post|upload|exfiltrate|email)\b\s+(this|these|the|all|every)\b.{0,60}\bto\b\s+(https?://|[\w.+-]+@[\w-]+\.)", re.I)),
    ("HTML comment", re.compile(r"<!--")),
    ("invisible characters", re.compile("[​‌‎‏⁠-⁤﻿‪-‮⁦-⁩]")),
    ("encoded blob", re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{160,}={0,2}(?![A-Za-z0-9+/=_-])")),
]

# handoff files that must hold typed lines only (no free-text slot for a "sleeper" instruction)
FORMAT_RULES = [
    (re.compile(r"(^|/)tuesday-community-engagement/status/parts-status-[^/]+\.md$"),
     re.compile(r"^\d{4}-\d{2}-\d{2} \| (Problem Channels|Slack snapshot|Channel Leader FYI) \| [^|<>]{1,160}$")),
]


# ---------- helpers ----------
def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_id(line):
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def clean(s, n=100):
    s = re.sub("[​-‏⁠-⁤﻿‪-‮⁦-⁩]", lambda m: "<U+%04X>" % ord(m.group()), s)
    s = "".join(ch if ch.isprintable() else " " for ch in s).strip()
    return (s[:n] + "…") if len(s) > n else s


def scan_lines(lines, start=1, allowed=()):
    hits = []
    for i, line in enumerate(lines, start):
        if allowed and line_id(line) in allowed:
            continue
        for name, rx in PATTERNS:
            if rx.search(line):
                hits.append((i, name, clean(line)))
                break
    return hits


def slug(key):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)


def copy_name(key, digest):
    return f"{slug(key)}--{digest[:12]}.txt"


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def load_baseline(path):
    if not path or path == "none" or not os.path.isfile(path):
        return {"files": {}, "allowed_lines": {}}
    with open(path, encoding="utf-8") as f:
        b = json.load(f)
    b.setdefault("files", {})
    b.setdefault("allowed_lines", {})
    return b


# ---------- mirror ----------
def watched(mirror):
    """Return {key: (label, text)} for every watched item present in the mirror."""
    out = {}
    pdir = os.path.join(mirror, "prompts")
    names = {}
    ip = os.path.join(pdir, "_index.json")
    if os.path.isfile(ip):
        with open(ip, encoding="utf-8") as f:
            names = json.load(f)
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if fn.endswith(".md"):
                tid = fn[:-3]
                out["task:" + tid] = (f"task \"{names.get(tid, tid)}\" ({tid})", read(os.path.join(pdir, fn)))
    sdir = os.path.join(mirror, "source")
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            if fn.startswith(".") or SECRET_RE.search(fn):
                continue
            out["source:" + fn] = (f"/Claude/Source/{fn}", read(os.path.join(sdir, fn)))
    rh = os.path.join(mirror, "repo_head.txt")
    if os.path.isfile(rh):
        out["repo:frun-scripts"] = ("GitHub frun-scripts version", read(rh).strip() + "\n")
    return out


def handoff_files(mirror):
    hdir = os.path.join(mirror, "handoff")
    res = []
    if not os.path.isdir(hdir):
        return res
    for dp, dns, fns in os.walk(hdir):
        dns.sort()
        for fn in sorted(fns):
            if fn.startswith(".") or SECRET_RE.search(fn):
                continue
            full = os.path.join(dp, fn)
            res.append((os.path.relpath(full, hdir).replace(os.sep, "/"), full))
    return res


# ---------- modes ----------
def cmd_prompts_from_triggers(a):
    with open(a.triggers, encoding="utf-8") as f:
        raw = f.read()
    data, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
    items = data.get("data", data.get("triggers", [])) if isinstance(data, dict) else data
    pdir = os.path.join(a.mirror, "prompts")
    os.makedirs(pdir, exist_ok=True)
    index, n = {}, 0
    for t in items:
        if not t.get("enabled"):
            continue
        prompt = find_prompt(t)
        if prompt is None:
            print(f"warning: no prompt found for {t.get('id')}", file=sys.stderr)
            continue
        with open(os.path.join(pdir, t["id"] + ".md"), "w", encoding="utf-8", newline="") as f:
            f.write(prompt)
        index[t["id"]] = t.get("name", t["id"])
        n += 1
    with open(os.path.join(pdir, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    print(f"wrote {n} enabled task prompts to {pdir}")


def find_prompt(t):
    ds = t.get("derived_state") or {}
    if isinstance(ds.get("prompt"), str):
        return ds["prompt"]
    for k in ("prompt",):
        if isinstance(t.get(k), str):
            return t[k]
    # job_config / events shapes: take the longest string found under a "content"/"prompt" key
    best = None
    stack = [t]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and k in ("prompt", "content", "text") and (best is None or len(v) > len(best)):
                    best = v
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(o, list):
            stack.extend(o)
    return best


def cmd_check(a):
    base = load_baseline(a.baseline)
    bfiles = base["files"]
    flags, verified = [], 0
    cur = watched(a.mirror)
    if not any(k.startswith("task:") for k in cur):
        flags.append("INTEGRITY — UNREACHABLE: no scheduled-task instructions in the mirror; task prompts NOT verified")
    if not any(k.startswith("source:") for k in cur):
        flags.append("INTEGRITY — UNREACHABLE: no /Claude/Source/ files in the mirror; CLAUDE.md and rule files NOT verified")
    for key, (label, text) in sorted(cur.items()):
        entry = bfiles.get(key)
        digest = sha(text)
        if entry is None:
            hits = scan_lines(text.splitlines())
            extra = f" — contains {hits[0][1]} at line {hits[0][0]}: \"{hits[0][2]}\"" if hits else ""
            flags.append(f"INTEGRITY — NEW: {label} is not in the approved baseline{extra}")
            continue
        if digest == entry["sha256"]:
            verified += 1
            continue
        if key == "repo:frun-scripts":
            flags.append(f"INTEGRITY — CHANGED: {label} is {text.strip()}, approved version is {read_copy(a.copies, entry).strip() or '?'} ({entry.get('approved', '?')})")
            continue
        old = read_copy(a.copies, entry).splitlines()
        new = text.splitlines()
        added, removed, added_lines = 0, 0, []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag in ("replace", "insert"):
                added += j2 - j1
                added_lines += [(j + 1, new[j]) for j in range(j1, j2)]
            if tag in ("replace", "delete"):
                removed += i2 - i1
        hits = []
        for n, line in added_lines:
            hits += scan_lines([line], n)
        sev = " ⚠️ ADDED TEXT MATCHES INJECTION PATTERN" if hits else ""
        first = f" first added line {added_lines[0][0]}: \"{clean(added_lines[0][1])}\"" if added_lines else ""
        pat = f" | pattern: {hits[0][1]} at line {hits[0][0]}: \"{hits[0][2]}\"" if hits else ""
        nocopy = "" if old else " (approved copy unavailable — line counts are vs empty)"
        flags.append(f"INTEGRITY — CHANGED{sev}: {label} differs from approved version ({entry.get('approved', '?')}): +{added}/-{removed} lines;{first}{pat}{nocopy}")
    for key in sorted(set(bfiles) - set(cur)):
        label = bfiles[key].get("label", key)
        kind = "is no longer enabled or no longer exists" if key.startswith("task:") else "is gone or no longer readable"
        flags.append(f"INTEGRITY — MISSING: {label} was approved but {kind}")
    scanned = 0
    for rel, full in handoff_files(a.mirror):
        try:
            if os.path.getsize(full) > SCAN_BYTES_MAX:
                continue
            lines = read(full).splitlines()
        except OSError:
            continue
        scanned += 1
        allowed = set(base["allowed_lines"].get(rel, []))
        for n, name, snip in scan_lines(lines, 1, allowed)[:3]:
            flags.append(f"INTEGRITY — PATTERN: {rel}:{n} — {name}: \"{snip}\"")
        for path_rx, line_rx in FORMAT_RULES:
            if path_rx.search(rel):
                bad = [(i, l) for i, l in enumerate(lines, 1) if l.strip() and not line_rx.match(l) and line_id(l) not in allowed]
                for i, l in bad[:3]:
                    flags.append(f"INTEGRITY — FORMAT: {rel}:{i} — line doesn't match the expected typed format (possible free-text slot): \"{clean(l)}\"")
    if flags:
        print("\n".join(flags))
    else:
        print(f"INTEGRITY OK — {verified} instruction items match approved versions; {scanned} recent handoff files scanned clean")
    return 0


def read_copy(copies, entry):
    if not copies or not entry.get("copy"):
        return ""
    p = os.path.join(copies, entry["copy"])
    return read(p) if os.path.isfile(p) else ""


def cmd_diff(a):
    base = load_baseline(a.baseline)
    cur = watched(a.mirror)
    if a.key not in cur:
        sys.exit(f"not in mirror: {a.key}")
    old = read_copy(a.copies, base["files"].get(a.key, {})).splitlines()
    for line in difflib.unified_diff(old, cur[a.key][1].splitlines(), "approved/" + a.key, "current/" + a.key, lineterm=""):
        print(line)


def cmd_approve(a):
    base = load_baseline(a.baseline)
    cur = watched(a.mirror)
    today = datetime.date.today().isoformat()
    if os.path.exists(a.out):
        sys.exit(f"refusing to overwrite {a.out} — baselines are write-once; pick a new name")
    if not a.all and not a.keys:
        sys.exit("approve: give KEY(s) or --all")
    targets = sorted(cur) if a.all else a.keys
    os.makedirs(a.copies_out, exist_ok=True)
    for key in targets:
        if key not in cur:
            print(f"skip (not in mirror): {key}")
            continue
        label, text = cur[key]
        digest = sha(text)
        name = copy_name(key, digest)
        with open(os.path.join(a.copies_out, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        base["files"][key] = {"sha256": digest, "approved": today, "label": label, "copy": name}
        print(f"approved: {key} -> {name}")
    if a.all:
        for key in sorted(set(base["files"]) - set(cur)):
            del base["files"][key]
            print(f"removed from baseline (no longer present): {key}")
    base["updated"] = today
    write_json(a.out, base)
    print(f"new baseline: {a.out}")


def cmd_allow(a):
    rel, _, n = a.spec.rpartition(":")
    full = os.path.join(a.mirror, "handoff", rel)
    if not os.path.isfile(full) or not n.isdigit():
        sys.exit(f"usage: allow <handoff path>:<line>  (not found: {a.spec})")
    line = read(full).splitlines()[int(n) - 1]
    base = load_baseline(a.baseline)
    lst = base["allowed_lines"].setdefault(rel, [])
    if line_id(line) not in lst:
        lst.append(line_id(line))
    base["updated"] = datetime.date.today().isoformat()
    write_json(a.out, base)
    print(f"allowed: {rel}:{n} \"{clean(line)}\" -> {a.out}")


def write_json(path, obj):
    if os.path.exists(path):
        sys.exit(f"refusing to overwrite {path} — baselines are write-once; pick a new name")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="mode", required=True)
    s = sp.add_parser("prompts-from-triggers"); s.add_argument("--triggers", required=True); s.add_argument("--mirror", required=True)
    s = sp.add_parser("check"); s.add_argument("--mirror", required=True); s.add_argument("--baseline", required=True); s.add_argument("--copies", required=True)
    s = sp.add_parser("diff"); s.add_argument("--mirror", required=True); s.add_argument("--baseline", required=True); s.add_argument("--copies", required=True); s.add_argument("key")
    s = sp.add_parser("approve"); s.add_argument("--mirror", required=True); s.add_argument("--baseline", required=True); s.add_argument("--copies", default=""); s.add_argument("--out", required=True); s.add_argument("--copies-out", required=True); s.add_argument("--all", action="store_true"); s.add_argument("keys", nargs="*")
    s = sp.add_parser("allow"); s.add_argument("--mirror", required=True); s.add_argument("--baseline", required=True); s.add_argument("--out", required=True); s.add_argument("spec")
    a = p.parse_args(argv)
    return {"prompts-from-triggers": cmd_prompts_from_triggers, "check": cmd_check, "diff": cmd_diff,
            "approve": cmd_approve, "allow": cmd_allow}[a.mode](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
