#!/usr/bin/env python3
"""
Think Tank Monitor — daily tracking of new research/reports from top US think tanks
via RSS/Atom feeds. Designed for deployment on an overseas VPS.

10 think tanks covering international security, national security, great power
strategy, and international political economy.

Usage:
  python3 thinktank_monitor.py          # fetch + email new items
  python3 thinktank_monitor.py --dry    # fetch only, print to stdout (no email)

Deploy to VPS:
  1. scp this file to VPS: scp thinktank_monitor.py user@vps:~/
  2. Set QQ SMTP auth code: export QQ_SMTP_AUTH_CODE="your..."
  3. Test: python3 thinktank_monitor.py --dry
  4. Cron: 0 9 * * * QQ_SMTP_AUTH_CODE=*** python3 /home/user/thinktank_monitor.py
"""

import os
import sys
import re
import json
import time
import hashlib
import textwrap
import urllib.request
import xml.etree.ElementTree as ET
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────────────────────────────

SENDER = os.environ.get("QQ_SMTP_SENDER", "1821339784@qq.com")
AUTH_CODE=os.environ.get("QQ_SMTP_AUTH_CODE", "")
RECIPIENT = "1821339784@qq.com"

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587

# State file — tracks seen items so we only email new ones
STATE_FILE = Path(__file__).resolve().parent / ".thinktank_monitor_state.json"

# ── THINK TANKS ─────────────────────────────────────────────────────────────
# (name, rss_url, focus_area)

THINK_TANKS = [
    # International Security & Strategy
    (
        "CSIS",
        "https://www.csis.org/rss.xml",
        "International Security & Strategy",
    ),
    (
        "RAND Corporation",
        "https://www.rand.org/rss/research.xml",
        "International Security & Strategy",
    ),
    (
        "CNAS",
        "https://www.cnas.org/press/rss",
        "International Security & Strategy",
    ),
    (
        "Hudson Institute",
        "https://www.hudson.org/rss",
        "International Security & Strategy",
    ),
    (
        "Atlantic Council",
        "https://www.atlanticcouncil.org/feed/",
        "International Security & Strategy",
    ),
    (
        "Stimson Center",
        "https://www.stimson.org/feed/",
        "International Security & Strategy",
    ),

    # Great Power Strategy & Geopolitics
    (
        "CFR",
        "https://www.cfr.org/feed",
        "Great Power Strategy & Geopolitics",
    ),
    (
        "Carnegie Endowment",
        "https://carnegieendowment.org/rss",
        "Great Power Strategy & Geopolitics",
    ),

    # International Political Economy & Governance
    (
        "Brookings Institution",
        "https://www.brookings.edu/feed/",
        "International Political Economy",
    ),
    (
        "AEI Foreign & Defense Policy",
        "https://www.aei.org/foreign-and-defense-policy/feed/",
        "International Political Economy",
    ),
]


# ── HELPERS ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load set of seen item hashes."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_hashes": [], "last_run": None}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def item_hash(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def strip_html(html_text: str) -> str:
    """Strip HTML tags, decode entities."""
    text = re.sub(r"<[^>]+>", "", html_text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", "\"").replace("&#039;", "'").replace("&nbsp;", " ")
    text = re.sub(r"&[a-z]+;", "", text)
    return text.strip()


def parse_date(date_str: str) -> str:
    """Parse various RSS date formats to YYYY-MM-DD."""
    if not date_str:
        return "?"
    # Common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            continue
    return "?"


def fetch_rss(url: str) -> list[dict]:
    """Fetch and parse RSS/Atom feed. Returns list of item dicts."""
    items = []
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ThinkTankMonitor/1.0 (mailto:1821339784@qq.com)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            root = ET.fromstring(content)
    except Exception as e:
        print(f"  [ERROR] {url}: {e}", file=sys.stderr)
        return []

    # Detect RSS vs Atom
    if root.tag == "rss":
        # RSS 2.0
        for item_el in root.findall(".//item"):
            title_el = item_el.find("title")
            link_el = item_el.find("link")
            desc_el = item_el.find("description")
            date_el = item_el.find("pubDate")

            title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            pub_date = parse_date(date_el.text) if date_el is not None and date_el.text else "?"

            # Clean description
            description = strip_html(description)
            description = textwrap.shorten(description, width=300, placeholder="...")

            items.append({
                "title": title,
                "link": link,
                "published": pub_date,
                "summary": description,
                "hash": item_hash(link),
            })
    else:
        # Atom feed
        ns_atom = "http://www.w3.org/2005/Atom"
        # Try both with and without namespace prefix
        entries = (root.findall(f"{{{ns_atom}}}entry") or
                   root.findall("entry"))

        for entry_el in entries:
            title_el = (entry_el.find(f"{{{ns_atom}}}title") or
                        entry_el.find("title"))
            link_el = (entry_el.find(f"{{{ns_atom}}}link") or
                       entry_el.find("link"))
            summary_el = (entry_el.find(f"{{{ns_atom}}}summary") or
                          entry_el.find("summary"))
            published_el = (entry_el.find(f"{{{ns_atom}}}published") or
                            entry_el.find("published") or
                            entry_el.find(f"{{{ns_atom}}}updated") or
                            entry_el.find("updated"))

            title = title_el.text.strip() if title_el is not None and title_el.text else "Untitled"
            link = link_el.get("href", "") if link_el is not None else ""
            summary = summary_el.text.strip() if summary_el is not None and summary_el.text else ""

            pub_date = "?"
            if published_el is not None and published_el.text:
                pub_date = parse_date(published_el.text)

            summary = strip_html(summary)
            summary = textwrap.shorten(summary, width=300, placeholder="...")

            items.append({
                "title": title,
                "link": link,
                "published": pub_date,
                "summary": summary,
                "hash": item_hash(link),
            })

    return items


def build_html(new_items: dict[str, dict[str, list[dict]]]) -> str:
    """Build HTML email grouped by focus area → think tank."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    total = sum(len(ilist) for area_data in new_items.values() for ilist in area_data.values())
    area_count = len(new_items)
    tank_count = sum(len(tdict) for tdict in new_items.values())

    html = f"""\
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #eff6ff; padding: 20px; color: #1e3a5f; }}
    .container {{ max-width: 740px; margin: 0 auto; background: #fff;
                  border-radius: 10px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); }}
    .header {{ background: linear-gradient(135deg, #1e3a5f, #2563eb); color: white;
               padding: 28px 30px 22px; border-radius: 10px 10px 0 0; }}
    .header h1 {{ margin: 0 0 4px; font-size: 21px; }}
    .header p {{ margin: 0; opacity: 0.85; font-size: 13px; }}
    .body {{ padding: 20px 30px 30px; }}
    .area-section {{ margin-bottom: 28px; }}
    .area-title {{ color: #1e3a5f; font-size: 17px; border-bottom: 2px solid #3b82f6;
                   padding-bottom: 5px; margin-bottom: 14px; }}
    .tank-block {{ margin-bottom: 16px; }}
    .tank-name {{ font-weight: 700; font-size: 14px; color: #475569; margin-bottom: 6px; }}
    .item {{ margin-bottom: 12px; padding: 10px 12px; border-left: 3px solid #e2e8f0;
             background: #f8fafc; border-radius: 0 4px 4px 0; }}
    .item-title {{ font-weight: 600; font-size: 14px; margin: 0 0 3px; line-height: 1.4; }}
    .item-title a {{ color: #1d4ed8; text-decoration: none; }}
    .item-title a:hover {{ text-decoration: underline; }}
    .item-meta {{ font-size: 11px; color: #94a3b8; }}
    .item-summary {{ font-size: 12px; color: #64748b; margin-top: 4px; line-height: 1.5; }}
    .badge {{ display: inline-block; background: #dbeafe; color: #1d4ed8;
              font-size: 10px; padding: 2px 7px; border-radius: 3px; }}
    .footer {{ background: #f1f5f9; padding: 16px 30px; border-radius: 0 0 10px 10px;
               font-size: 11px; color: #94a3b8; text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🏛️ Think Tank Monitor</h1>
      <p>{today_str} &nbsp;|&nbsp; {total} new reports from {tank_count} think tanks</p>
    </div>
    <div class="body">
"""

    for area_name, tanks in new_items.items():
        area_total = sum(len(v) for v in tanks.values())
        if area_total == 0:
            continue
        html += f'    <div class="area-section">\n'
        html += f'      <h2 class="area-title">{area_name} ({area_total} reports)</h2>\n'
        for tank_name, items in tanks.items():
            if not items:
                continue
            html += f'      <div class="tank-block">\n'
            html += f'        <div class="tank-name">🏛️ {tank_name}</div>\n'
            for item in items[:5]:  # Max 5 per tank to keep email compact
                html += f'        <div class="item">\n'
                html += f'          <p class="item-title"><a href="{item["link"]}">{item["title"]}</a></p>\n'
                html += f'          <p class="item-meta">{item["published"]}</p>\n'
                if item["summary"]:
                    html += f'          <p class="item-summary">{item["summary"]}</p>\n'
                html += f'        </div>\n'
            html += f'      </div>\n'
        html += f'    </div>\n'

    html += """\
    </div>
    <div class="footer">
      Think Tank Monitor — deployed on overseas VPS · daily automated
    </div>
  </div>
</body>
</html>"""
    return html


def build_plain(new_items: dict[str, dict[str, list[dict]]]) -> str:
    """Plain text version."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"Think Tank Monitor — {today_str}", "=" * 50, ""]

    for area_name, tanks in new_items.items():
        area_total = sum(len(v) for v in tanks.values())
        if area_total == 0:
            continue
        lines.append(f"── {area_name} ({area_total} reports) ──")
        for tank_name, items in tanks.items():
            if not items:
                continue
            lines.append(f"  [{tank_name}]")
            for item in items:
                lines.append(f"    • {item['title']}")
                lines.append(f"      {item['published']} | {item['link']}")
            lines.append("")

    lines.append("-" * 50)
    lines.append("Think Tank Monitor — VPS deployment")
    return "\n".join(lines)


def send_email(html_body: str, plain_body: str, subject: str) -> bool:
    if not AUTH_CODE:
        print("[ERROR] QQ_SMTP_AUTH_CODE not set!", file=sys.stderr)
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Think Tank Monitor <{SENDER}>"
    msg["To"] = RECIPIENT
    msg["Subject"] = subject

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(SENDER, AUTH_CODE)
            server.sendmail(SENDER, RECIPIENT, msg.as_string())
        print(f"[OK] Email sent to {RECIPIENT}")
        return True
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return False


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry" in sys.argv or "--dry-run" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"=== Think Tank Monitor — {today} ===")
    print(f"Tracking {len(THINK_TANKS)} think tanks\n")

    state = load_state()
    seen = set(state.get("seen_hashes", []))
    last_run = state.get("last_run", "never")
    print(f"Last run: {last_run} | Known items: {len(seen)}")

    # Fetch all feeds
    all_items = []
    for name, url, area in THINK_TANKS:
        print(f"  [{name}] fetching...", end=" ", flush=True)
        items = fetch_rss(url)
        new_items = [i for i in items if i["hash"] not in seen]
        print(f"{len(items)} total, {len(new_items)} new")
        all_items.append((name, area, new_items))
        time.sleep(1)  # Be polite to servers

    # Group by area
    new_by_area: dict[str, dict[str, list[dict]]] = {}
    total_new = 0
    for name, area, items in all_items:
        if items:
            total_new += len(items)
            if area not in new_by_area:
                new_by_area[area] = {}
            new_by_area[area][name] = items
            for i in items:
                seen.add(i["hash"])

    print(f"\nNew items: {total_new}")

    if dry_run:
        print("\n=== DRY RUN — No email sent ===\n")
        for area_name, tanks in new_by_area.items():
            print(f"── {area_name} ──")
            for tank_name, items in tanks.items():
                print(f"  [{tank_name}]")
                for item in items:
                    print(f"    • {item['title']}")
                    print(f"      {item['published']} | {item['link']}")
                print()
        return

    if total_new == 0:
        print("No new items — skipping email.")
        # Update state even if no new items
        state["last_run"] = today
        save_state(state)
        return

    # Build and send email
    subject = f"🏛️ Think Tank Monitor — {today} ({total_new} new reports)"
    html_body = build_html(new_by_area)
    plain_body = build_plain(new_by_area)

    success = send_email(html_body, plain_body, subject)
    if success:
        state["seen_hashes"] = list(seen)
        state["last_run"] = today
        save_state(state)
        print("Done!")
    else:
        print("Email failed, state not updated.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
