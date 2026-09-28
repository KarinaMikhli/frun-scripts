# frun-scripts

Code used by Karina's FrUn cloud scheduled tasks. **Code only — no data, no keys.**

- `archive/` — Slack + Fathom archive scripts (daily builders, activity scan, monthly rollup, `slack_md.py`) and their tests.
- `channel-directory/` — `generate.py`, builds the member-facing Slack channel directory (HTML).
- `integrity/` — `integrity_check.py`, the instruction-integrity check (cloud port of the Mac health-check script, 2026-09-28). Flags any scheduled-task instructions or `/Claude/Source/` rule file that no longer matches the version Karina approved, plus injection-style text in recent handoff files. Approved versions live in Dropbox (`/Claude/Scheduled-Data/scheduled-task-health-check/integrity/`), write-once. `approve`/`allow` are for live sessions with Karina only — never scheduled runs.

## Rules
- Scheduled runs **read** this repo (clone it) and never push, edit or commit to it.
- Every change here is made by a person (with Claude's help) and reviewed as a diff before it's merged.
- All data — archives, state files, logs — lives in Dropbox, not here.

Run the tests: `cd archive && python3 -m unittest discover -s tests` and `cd integrity && python3 -m unittest discover -s tests`
