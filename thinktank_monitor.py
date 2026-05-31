#!/usr/bin/env python3
"""
Think Tank Monitor v3 — comprehensive daily tracking of 10 top US think tanks.

Strategy (dual-mode per think tank):
  1. Try RSS/Atom feed first
  2. If RSS fails or returns stale content, scrape the website's latest articles

Deployment: GitHub Actions, daily at 1:00 UTC (9:00 AM Beijing).
"""

import os, sys, re, json, time, hashlib, textwrap
import urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
import smtplib, ssl
from html import unescape as html_unescape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────────────────────────────

SENDER = os.environ.get("QQ_SMTP_SENDER", "1821339784@qq.com")
AUTH_CODE = os.environ.get("QQ_SMTP_AUTH_CODE", "")
RECIPIENT = "1821339784@qq.com"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587
STATE_FILE = Path(__file__).resolve().parent / ".thinktank_monitor_state.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── HELPERS ─────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_hashes": [], "last_run": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def item_hash(link):
    return hashlib.sha256(link.encode()).hexdigest()[:16]

def strip_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = html_unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def parse_date(date_str):
    if not date_str:
        return "?"
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y",
        "%m/%d/%Y", "%d %B %Y", "%d %b %Y",
    ]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            continue
    return "?"

def _fetch(url, timeout=20):
    """Fetch URL with browser-like headers."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        return None

def _sanitize_xml(raw_bytes):
    """Resolve HTML entities for XML parsing."""
    text = raw_bytes.decode("utf-8", errors="replace")
    # Protect XML core entities
    for code, char in [("&amp;", "\x00AMP\x00"), ("&lt;", "\x00LT\x00"),
                        ("&gt;", "\x00GT\x00"), ("&quot;", "\x00QUOT\x00")]:
        text = text.replace(char, code)
    text = html_unescape(text)
    for code, char in [("\x00AMP\x00", "&amp;"), ("\x00LT\x00", "&lt;"),
                        ("\x00GT\x00", "&gt;"), ("\x00QUOT\x00", "&quot;")]:
        text = text.replace(char, code)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    return text.encode("utf-8")

def parse_rss(content_bytes):
    """Parse RSS/Atom and return list of {title, link, published, summary, hash}."""
    items = []
    try:
        content = _sanitize_xml(content_bytes)
        root = ET.fromstring(content)
    except ET.ParseError:
        return []

    if root.tag == "rss":
        for el in root.findall(".//item"):
            t = el.find("title"); l = el.find("link")
            d = el.find("pubDate"); s = el.find("description")
            title = t.text.strip() if t is not None and t.text else "Untitled"
            link = l.text.strip() if l is not None and l.text else ""
            pub = parse_date(d.text) if d is not None and d.text else "?"
            summary = textwrap.shorten(strip_html(s.text if s is not None and s.text else ""), width=300, placeholder="...")
            if link:
                items.append({"title": title, "link": link, "published": pub, "summary": summary, "hash": item_hash(link)})
    else:
        ns = "http://www.w3.org/2005/Atom"
        entries = root.findall(f"{{{ns}}}entry") or root.findall("entry")
        for el in entries:
            t = el.find(f"{{{ns}}}title") or el.find("title")
            l = el.find(f"{{{ns}}}link") or el.find("link")
            s = el.find(f"{{{ns}}}summary") or el.find("summary")
            p = el.find(f"{{{ns}}}published") or el.find("published") or el.find(f"{{{ns}}}updated") or el.find("updated")
            title = t.text.strip() if t is not None and t.text else "Untitled"
            link = l.get("href", "") if l is not None else ""
            pub = parse_date(p.text) if p is not None and p.text else "?"
            summary = textwrap.shorten(strip_html(s.text if s is not None and s.text else ""), width=300, placeholder="...")
            if link:
                items.append({"title": title, "link": link, "published": pub, "summary": summary, "hash": item_hash(link)})
    return items

# ── PER-THINK-TANK SCRAPERS ─────────────────────────────────────────────────
# Each returns list of {title, link, published, summary, hash}

def scrape_csis():
    """CSIS: Drupal. RSS /rss.xml exists but may return stale content.
    Scrape /analysis and /commentary pages."""
    items = []
    # CSIS main RSS (may have old content, but try as fallback)
    for url in [
        "https://www.csis.org/analysis",
        "https://www.csis.org/commentary",
    ]:
        html = _fetch(url)
        if not html:
            continue
        text = html.decode("utf-8", errors="replace")
        # CSIS article cards: <article> with <h2><a> title, <time> date
        # Pattern 1: Drupal article teasers
        for m in re.finditer(
            r'<h2[^>]*>\s*<a[^>]*href="(/[^"]+)"[^>]*>([^<]+)</a>',
            text, re.IGNORECASE
        ):
            link = "https://www.csis.org" + m.group(1)
            title = strip_html(m.group(2))
            if title and link and "/analysis/" in link or "/commentary/" in link:
                items.append({"title": title, "link": link, "published": "?", "summary": "", "hash": item_hash(link)})
        # Pattern 2: time elements near the title
        for m in re.finditer(
            r'<time[^>]*datetime="([^"]+)"[^>]*>',
            text, re.IGNORECASE
        ):
            pass  # Match with preceding title
    return items[:30]

def scrape_rand():
    """RAND: RSS removed. Scrape /research page."""
    items = []
    html = _fetch("https://www.rand.org/research.html")
    if not html:
        return items
    text = html.decode("utf-8", errors="replace")
    # RAND research cards
    for m in re.finditer(
        r'href="(/pubs/[^"]+)"[^>]*>(?:.*?<h3[^>]*>([^<]+)</h3>)?',
        text, re.IGNORECASE
    ):
        link = "https://www.rand.org" + m.group(1)
        title = strip_html(m.group(2) or "")
        if title and "/pubs/" in link:
            items.append({"title": title, "link": link, "published": "?", "summary": "", "hash": item_hash(link)})
    if not items:
        # Try scraping the main pubs listing page
        html2 = _fetch("https://www.rand.org/pubs.html")
        if html2:
            text2 = html2.decode("utf-8", errors="replace")
            for m in re.finditer(r'href="(/pubs/[^"]+)"[^>]*>([^<]+)<', text2):
                link = "https://www.rand.org" + m.group(1)
                title = strip_html(m.group(2))
                if title and "/pubs/" in link:
                    items.append({"title": title, "link": link, "published": "?", "summary": "", "hash": item_hash(link)})
    return items[:30]

def scrape_cnas():
    """CNAS: ExpressionEngine. Try RSS /feed, scrape as fallback."""
    items = []
    # Try RSS
    content = _fetch("https://www.cnas.org/feed")
    if content:
        items = parse_rss(content)
    if not items:
        # Scrape publications page
        html = _fetch("https://www.cnas.org/publications")
        if html:
            text = html.decode("utf-8", errors="replace")
            for m in re.finditer(r'<a[^>]*href="(/publications/[^"]+)"[^>]*>([^<]+)</a>', text):
                link = "https://www.cnas.org" + m.group(1)
                title = strip_html(m.group(2))
                if title:
                    items.append({"title": title, "link": link, "published": "?", "summary": "", "hash": item_hash(link)})
    return items[:30]

def scrape_hudson():
    """Hudson Institute: WordPress. Try /feed/ and WP REST API."""
    items = []
    # Try WordPress REST API (most reliable)
    api_url = "https://www.hudson.org/wp-json/wp/v2/posts?per_page=20"
    try:
        req = urllib.request.Request(api_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            for post in data:
                title = strip_html(post.get("title", {}).get("rendered", ""))
                link = post.get("link", "")
                pub = post.get("date", "?")[:10] if post.get("date") else "?"
                summary = textwrap.shorten(strip_html(post.get("excerpt", {}).get("rendered", "")), 300, placeholder="...")
                if link:
                    items.append({"title": title, "link": link, "published": pub, "summary": summary, "hash": item_hash(link)})
    except Exception:
        pass
    if not items:
        # Fallback to RSS
        content = _fetch("https://www.hudson.org/feed/")
        if content:
            items = parse_rss(content)
    return items[:30]

def scrape_atlantic_council():
    """Atlantic Council: WordPress. RSS works well."""
    content = _fetch("https://www.atlanticcouncil.org/feed/")
    if content:
        return parse_rss(content)[:30]
    return []

def scrape_stimson():
    """Stimson Center: Cloudflare blocks RSS. Try WP REST API."""
    items = []
    api_url = "https://www.stimson.org/wp-json/wp/v2/posts?per_page=20"
    try:
        req = urllib.request.Request(api_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            for post in data:
                title = strip_html(post.get("title", {}).get("rendered", ""))
                link = post.get("link", "")
                pub = post.get("date", "?")[:10] if post.get("date") else "?"
                summary = textwrap.shorten(strip_html(post.get("excerpt", {}).get("rendered", "")), 300, placeholder="...")
                if link:
                    items.append({"title": title, "link": link, "published": pub, "summary": summary, "hash": item_hash(link)})
    except Exception:
        pass
    return items[:30]

def scrape_cfr():
    """CFR: Custom CMS, RSS /feed works."""
    content = _fetch("https://www.cfr.org/feed")
    if content:
        return parse_rss(content)[:30]
    return []

def scrape_carnegie():
    """Carnegie Endowment: New Next.js site, old RSS gone. Scrape latest page."""
    items = []
    # Try the new site's API or scrape
    html = _fetch("https://carnegieendowment.org/research")
    if html:
        text = html.decode("utf-8", errors="replace")
        # Next.js site - look for article links
        for m in re.finditer(r'<a[^>]*href="(/research/[^"]+)"[^>]*>(.*?)</a>', text, re.DOTALL):
            link = "https://carnegieendowment.org" + m.group(1)
            title_html = m.group(2)
            title = strip_html(title_html)
            if title and len(title) > 10:
                items.append({"title": title, "link": link, "published": "?", "summary": "", "hash": item_hash(link)})
    return items[:30]

def scrape_brookings():
    """Brookings: WordPress. RSS /feed/ has XML entity issues. Use WP API."""
    items = []
    api_url = "https://www.brookings.edu/wp-json/wp/v2/posts?per_page=20"
    try:
        req = urllib.request.Request(api_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            for post in data:
                title = strip_html(post.get("title", {}).get("rendered", ""))
                link = post.get("link", "")
                pub = post.get("date", "?")[:10] if post.get("date") else "?"
                summary = textwrap.shorten(strip_html(post.get("excerpt", {}).get("rendered", "")), 300, placeholder="...")
                if link:
                    items.append({"title": title, "link": link, "published": pub, "summary": summary, "hash": item_hash(link)})
    except Exception:
        pass
    if not items:
        content = _fetch("https://www.brookings.edu/feed/")
        if content:
            items = parse_rss(content)
    return items[:30]

def scrape_aei():
    """AEI: WordPress. Try WP API and RSS."""
    items = []
    api_url = "https://www.aei.org/wp-json/wp/v2/posts?per_page=20"
    try:
        req = urllib.request.Request(api_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            for post in data:
                title = strip_html(post.get("title", {}).get("rendered", ""))
                link = post.get("link", "")
                pub = post.get("date", "?")[:10] if post.get("date") else "?"
                summary = textwrap.shorten(strip_html(post.get("excerpt", {}).get("rendered", "")), 300, placeholder="...")
                if link:
                    items.append({"title": title, "link": link, "published": pub, "summary": summary, "hash": item_hash(link)})
    except Exception:
        pass
    if not items:
        content = _fetch("https://www.aei.org/feed/")
        if content:
            items = parse_rss(content)
    return items[:30]

# ── THINK TANK REGISTRY ─────────────────────────────────────────────────────

THINK_TANKS = [
    ("CSIS",                   scrape_csis,             "International Security & Strategy"),
    ("RAND Corporation",       scrape_rand,             "International Security & Strategy"),
    ("CNAS",                   scrape_cnas,             "International Security & Strategy"),
    ("Hudson Institute",       scrape_hudson,           "International Security & Strategy"),
    ("Atlantic Council",       scrape_atlantic_council, "International Security & Strategy"),
    ("Stimson Center",         scrape_stimson,          "International Security & Strategy"),
    ("CFR",                    scrape_cfr,              "Great Power Strategy & Geopolitics"),
    ("Carnegie Endowment",     scrape_carnegie,         "Great Power Strategy & Geopolitics"),
    ("Brookings Institution",  scrape_brookings,        "International Political Economy"),
    ("AEI",                    scrape_aei,              "International Political Economy"),
]

# ── EMAIL ───────────────────────────────────────────────────────────────────

def build_html(new_items):
    today_str = datetime.now().strftime("%Y-%m-%d")
    total = sum(len(ilist) for area in new_items.values() for ilist in area.values())
    tank_count = sum(1 for area in new_items.values() for ilist in area.values() if ilist)

    html = f"""<html><head><meta charset="utf-8"><style>
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#eff6ff;padding:20px;color:#1e3a5f}}
    .container{{max-width:740px;margin:0 auto;background:#fff;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
    .header{{background:linear-gradient(135deg,#1e3a5f,#2563eb);color:#fff;padding:28px 30px 22px;border-radius:10px 10px 0 0}}
    .header h1{{margin:0 0 4px;font-size:21px}}
    .header p{{margin:0;opacity:.85;font-size:13px}}
    .body{{padding:20px 30px 30px}}
    .area-section{{margin-bottom:28px}}
    .area-title{{color:#1e3a5f;font-size:17px;border-bottom:2px solid #3b82f6;padding-bottom:5px;margin-bottom:14px}}
    .tank-block{{margin-bottom:16px}}
    .tank-name{{font-weight:700;font-size:14px;color:#475569;margin-bottom:6px}}
    .item{{margin-bottom:12px;padding:10px 12px;border-left:3px solid #e2e8f0;background:#f8fafc;border-radius:0 4px 4px 0}}
    .item-title{{font-weight:600;font-size:14px;margin:0 0 3px;line-height:1.4}}
    .item-title a{{color:#1d4ed8;text-decoration:none}}
    .item-title a:hover{{text-decoration:underline}}
    .item-meta{{font-size:11px;color:#94a3b8}}
    .item-summary{{font-size:12px;color:#64748b;margin-top:4px;line-height:1.5}}
    .footer{{background:#f1f5f9;padding:16px 30px;border-radius:0 0 10px 10px;font-size:11px;color:#94a3b8;text-align:center}}
    .summary-stats{{background:#f0f9ff;border:1px solid #bae6fd;border-radius:6px;padding:10px 16px;margin-bottom:20px;font-size:13px}}
    .summary-stats b{{color:#0369a1}}
    </style></head><body><div class="container">
    <div class="header"><h1>🏛️ Think Tank Monitor</h1>
    <p>{today_str} &nbsp;|&nbsp; {total} new reports from {tank_count} think tanks</p></div>
    <div class="body">"""

    for area_name, tanks in new_items.items():
        area_total = sum(len(v) for v in tanks.values())
        if area_total == 0:
            continue
        html += f'<div class="area-section"><h2 class="area-title">{area_name} ({area_total} reports)</h2>'
        for tank_name, items in tanks.items():
            if not items:
                continue
            html += f'<div class="tank-block"><div class="tank-name">🏛️ {tank_name} ({len(items)})</div>'
            for item in items[:5]:
                html += f'<div class="item"><p class="item-title"><a href="{item["link"]}">{item["title"]}</a></p>'
                html += f'<p class="item-meta">{item["published"]}</p>'
                if item["summary"]:
                    html += f'<p class="item-summary">{item["summary"]}</p>'
                html += '</div>'
            html += '</div>'
        html += '</div>'

    html += '</div><div class="footer">Think Tank Monitor v3 · GitHub Actions daily · <a href="https://github.com/qzeng-dev/thinktank-monitor">repo</a></div></div></body></html>'
    return html

def build_plain(new_items):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"Think Tank Monitor — {today}", "=" * 50, ""]
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
    lines.append("Think Tank Monitor v3 — GitHub Actions")
    return "\n".join(lines)

def send_email(html_body, plain_body, subject):
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
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo(); s.starttls(context=ctx); s.ehlo()
            s.login(SENDER, AUTH_CODE)
            s.sendmail(SENDER, RECIPIENT, msg.as_string())
        print(f"[OK] Email sent to {RECIPIENT}")
        return True
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return False

# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry" in sys.argv or "--dry-run" in sys.argv
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"=== Think Tank Monitor v3 — {today} ===")
    print(f"Tracking {len(THINK_TANKS)} think tanks (scrape-first strategy)\n")

    state = load_state()
    seen = set(state.get("seen_hashes", []))
    print(f"Last run: {state.get('last_run', 'never')} | Known items: {len(seen)}")

    all_results = []
    working = []
    failed = []

    for name, scraper_fn, area in THINK_TANKS:
        print(f"  [{name}] ", end="", flush=True)
        try:
            items = scraper_fn()
            new_items = [i for i in items if i["hash"] not in seen]
            print(f"{len(items)} fetched, {len(new_items)} new")
            all_results.append((name, area, new_items))
            if items:
                working.append(name)
            else:
                failed.append(name)
        except Exception as e:
            print(f"ERROR: {e}")
            failed.append(name)
            all_results.append((name, area, []))
        time.sleep(1)

    # Group by area
    new_by_area = {}
    total_new = 0
    for name, area, items in all_results:
        if items:
            total_new += len(items)
            new_by_area.setdefault(area, {})[name] = items
            for i in items:
                seen.add(i["hash"])

    print(f"\nResults: {len(working)}/{len(THINK_TANKS)} working, {total_new} new items")
    if working:
        print(f"  Working: {', '.join(working)}")
    if failed:
        print(f"  No content: {', '.join(failed)}")

    if dry_run:
        print("\n=== DRY RUN ===\n")
        for area_name, tanks in new_by_area.items():
            print(f"── {area_name} ──")
            for tank_name, items in tanks.items():
                print(f"  [{tank_name}]")
                for item in items[:3]:
                    print(f"    • {item['title'][:80]}")
                    print(f"      {item['published']} | {item['link'][:80]}")
                print()
        return

    if total_new == 0:
        print("No new items — skipping email.")
        state["last_run"] = today
        save_state(state)
        return

    subject = f"🏛️ Think Tank Monitor — {today} ({total_new} new reports)"
    html_body = build_html(new_by_area)
    plain_body = build_plain(new_by_area)

    if send_email(html_body, plain_body, subject):
        state["seen_hashes"] = list(seen)
        state["last_run"] = today
        save_state(state)
        print("Done!")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
