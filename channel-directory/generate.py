#!/usr/bin/env python3
"""
Generate the FrUn Slack channels visual (HTML + PDF) from channels.json.

Usage:
    python3 generate.py

Reads:
    ./channels.json
Writes:
    ../../Misc/frun-slack-channels.html
    ../../Misc/frun-slack-channels.pdf
"""
import json
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
# This folder lives in Fractionals United/Slack/_slack-channels-snapshot/.
# Outputs are kept in Fractionals United/Misc/ (HERE.parent.parent / "Misc").
OUT_DIR = HERE.parent.parent / "Misc"
DATA_PATH = HERE / "channels.json"
HTML_OUT = OUT_DIR / "frun-slack-channels.html"
PDF_OUT = OUT_DIR / "frun-slack-channels.pdf"

CSS = """
@page { size: A4 portrait; margin: 20px 28px; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'DM Sans', sans-serif; background: #f7f6f3; padding: 24px 28px 20px; width: 100%; }
.header { text-align: center; margin-bottom: 16px; }
.header h1 { font-family: 'DM Serif Display', serif; font-size: 24px; color: #1c1c22; margin-bottom: 4px; }
.header p { font-size: 12px; color: #5a5a6a; line-height: 1.4; }
.grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; align-items: start; }
.card { background: #ffffff; border-radius: 10px; border: 1px solid #e2e0db; overflow: hidden; }
.card-header { padding: 10px 14px 8px; display: flex; align-items: center; gap: 8px; }
.badge { font-size: 9.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; padding: 2px 8px; border-radius: 20px; white-space: nowrap; }
.badge-blue   { background: #4A63A0; color: #fff; }
.badge-orange { background: #E8A870; color: #1c1c22; }
.badge-teal   { background: #5A8A84; color: #fff; }
.badge-slate  { background: #7A8BA8; color: #fff; }
.badge-warm   { background: #C4806A; color: #fff; }
.badge-purple { background: #8A7AB8; color: #fff; }
.badge-moss   { background: #7A9A6A; color: #fff; }
.card-title { font-family: 'DM Serif Display', serif; font-size: 13px; color: #1c1c22; }
.card-desc { font-size: 11px; color: #5a5a6a; padding: 0 14px 9px; line-height: 1.4; }
.card-footnote { font-size: 9.5px; color: #8a8a96; font-style: italic; padding: 2px 14px 10px; line-height: 1.3; }
.divider { height: 1px; background: #f0eeea; margin: 0 14px; }
.ch       { padding: 8px 14px 11px; display: flex; flex-direction: column; gap: 4px; }
.ch-2col  { padding: 8px 14px 11px; display: grid; grid-template-columns: 1fr 1fr; gap: 4px 10px; }
.ch-5col  { padding: 8px 14px 11px; display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px 10px; }
.ch-3col  { padding: 8px 14px 11px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px 10px; }
.channel { display: flex; align-items: center; gap: 3px; font-size: 11px; }
.channel-hash { color: #c0bfd0; font-weight: 600; font-size: 11px; flex-shrink: 0; }
.channel-name { color: #2a2a3a; font-weight: 500; }
.tips { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 10px; }
.tip { background: #fff; border: 1px solid #e2e0db; border-radius: 8px; padding: 9px 13px; font-size: 11px; color: #5a5a6a; line-height: 1.4; }
.tip strong { color: #1c1c22; }
.footer { text-align: center; margin-top: 10px; font-size: 10px; color: #c0bfd0; }
"""

TIPS = [
    "<strong>Start here:</strong> Join all the <strong>1- main</strong> channels and at least one <strong>2-fr- function</strong> channel that matches your work.",
    "<strong>The numbering is intentional</strong> — channels sort automatically in Slack, so you'll always find things in the same place.",
    "<strong>Questions?</strong> Drop them in <strong>#0-member-help</strong>.",
]


def render_card(cat: dict) -> str:
    title_html = (
        f'<span class="card-title">{escape(cat["title"])}</span>'
        if cat.get("title") else ""
    )
    channels_html = "\n        ".join(
        f'<div class="channel"><span class="channel-hash">#</span><span class="channel-name">{escape(c)}</span></div>'
        for c in cat["channels"]
    )
    footnote_html = (
        f'<p class="card-footnote">{escape(cat["footnote"])}</p>'
        if cat.get("footnote") else ""
    )
    return f"""    <div class="card" style="grid-column: span {cat['span']};">
      <div class="card-header">
        <span class="badge {cat['badge_class']}">{escape(cat['badge_label'])}</span>
        {title_html}
      </div>
      <p class="card-desc">{cat['desc']}</p>
      <div class="divider"></div>
      <div class="{cat['layout']}">
        {channels_html}
      </div>
      {footnote_html}
    </div>"""


def render_html(data: dict) -> str:
    cards_html = "\n\n".join(render_card(c) for c in data["categories"])
    tips_html = "\n    ".join(f'<div class="tip">{t}</div>' for t in TIPS)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FrUn Slack Channels</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>

  <div class="header">
    <h1>Your FrUn Slack Channels</h1>
    <p>Channels are organized by prefix so they sort together naturally. Start with the main channels — then opt into what fits you.</p>
  </div>

  <div class="grid">

{cards_html}

  </div>

  <div class="tips">
    {tips_html}
  </div>

  <div class="footer">Fractionals United · fractionalsunited.com</div>

</body>
</html>
"""


def main():
    data = json.loads(DATA_PATH.read_text())
    html = render_html(data)
    HTML_OUT.write_text(html)
    print(f"Wrote {HTML_OUT}")

    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(str(PDF_OUT))
        print(f"Wrote {PDF_OUT}")
    except Exception as e:
        print(f"PDF generation skipped: {e}")


if __name__ == "__main__":
    main()
