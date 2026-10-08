#!/usr/bin/env python3
"""
Wednesday Wire — site builder.

Renders articles.json + fixtures.json into index.html: a clean, fast,
ad-free static page with light/dark mode, filters, search and the
fixtures/form strip. Also writes feed.xml, sitemap.xml, robots.txt,
version.json, manifest.webmanifest and status.html (feed health).

Run after fetch_news.py and fetch_fixtures.py. Deploy anywhere static
files go (GitHub Pages is free).

Safety: if there are too few articles to make a sensible page, nothing is
written and the script exits with an error, so the workflow stops before
deploying - the live site keeps its last good version.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
UK = ZoneInfo("Europe/London")
URL_SAFE_CHARS = ":/?#[]@!$&'()*+,;=%~-._"   # everything else in a link gets %-encoded

# The live address (used for social link previews, the RSS feed, the
# sitemap and restore_state.py). thewednesdaytimes.uk 301-redirects here.
SITE_URL = "https://wednesdaywire.co.uk"
SITE_NAME = "Wednesday Wire"

# Free, cookie-free analytics: https://www.goatcounter.com/ (no signup cost).
# Sign up, then put your code here (the bit before ".goatcounter.com").
# Leave blank to skip analytics entirely — nothing breaks either way.
GOATCOUNTER_CODE = "allwednesday"

KOFI_URL = "https://ko-fi.com/allwednesday"

# Below this many stories something has gone badly wrong upstream, so we
# refuse to build rather than deploy a near-empty site.
MIN_ARTICLES = 5
LEAD_STORIES = 3

# ---- story tagging (keyword-based, tune freely) ----
# Whole words only, so "ideal" isn't a transfer "deal", "reflects"
# isn't "EFL" and "signs up for the 10k" isn't a signing.
TAG_RULES = [
    ("Transfers", r"\btransfers?\b|\bsign(?:s|ed|ing|ings)?\b(?! up)|\bloan(?:s|ed|ee)?\b|\blinked\b"
                  r"|\bbids?\b|\bdeals?\b|\bcontracts?\b|\bfees?\b|\bswoop\b|\btargets?\b|\bfree agents?\b"),
    ("Match", r"\bhighlights\b|\breport\b|\bfull[- ]time\b|\bfriendly\b|\bkick[- ]off\b|\bline-?ups?\b"
              r"|\breaction\b|\bplayer ratings\b|\bpreview\b|\bteam news\b|\bpredicted\b"),
    ("Club news", r"\bstatement\b|\btickets?\b|\bannounce\w*|\bconfirm\w*|\bhillsborough\b|\bownership\b"
                  r"|\btakeover\b|\befl\b"),
]
_TAG_RES = [(tag, re.compile(rx, re.IGNORECASE)) for tag, rx in TAG_RULES]

# Official accounts — always visible under the header, links only.
OFFICIAL_LINKS = [
    ("Club site", "https://www.swfc.co.uk"),
    ("Instagram", "https://www.instagram.com/swfcofficial"),
    ("YouTube", "https://www.youtube.com/user/officialswfc"),
    ("EFL", "https://www.efl.com"),
]

# ---- source groupings, used for filter pills only (not sort order) ----
# Judgment call, not a formula - adjust freely.
SOURCE_GROUPS = {
    "BBC": "National", "Sky Sports": "National", "Goal.com": "National",
    "talkSPORT": "National", "The Sun": "National", "Inside Futbol": "National",
    "The72": "National", "hayters.com": "National",
    "The Star": "Regional", "Yorkshire Post": "Regional", "Sheffield Tribune": "Regional",
}

FILTER_REASONS = {
    "sheffield_united": "Sheffield United stories with no Wednesday angle",
    "off_topic": "Not about Wednesday (e.g. a former player at his new club)",
    "stream_spam": "Illegal live-stream spam",
    "betting": "Bookmaker promotions and betting tips",
    "junk_title": "Broken or empty headlines",
    "bad_link": "Links that weren't ordinary web addresses",
}


# =====================================================================
# Helpers
# =====================================================================

def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def safe_href(url) -> str | None:
    """http(s) only - a feed can never put a javascript: link on the page."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or re.search(r"[\s\x00-\x1f\x7f]", url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() in ("http", "https") and host and not (parts.username or parts.password):
        return quote(url, safe=URL_SAFE_CHARS)
    return None


def safe_img(url) -> str | None:
    url = safe_href(url)
    return url if url and url.lower().startswith("https://") else None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def parse_iso(iso: str) -> datetime:
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def tag_for(article: dict) -> str | None:
    text = f"{article['title']} {article.get('excerpt') or ''}"
    for tag, rx in _TAG_RES:
        if rx.search(text):
            return tag
    return None


def source_group(source: str) -> str:
    return SOURCE_GROUPS.get(source, "")


def rel_time(iso: str, now: datetime) -> str:
    mins = int((now - parse_iso(iso)).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def uk_date(iso: str):
    return parse_iso(iso).astimezone(UK).date()


def long_date(d) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def day_label(d, today) -> str:
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    return long_date(d)


def fmt_kickoff(iso: str, time_known: bool = True) -> str:
    dt = parse_iso(iso).astimezone(UK)
    day = f"{dt:%a} {dt.day} {dt:%b}"
    return f"{day}, {dt:%H:%M}" if time_known else f"{day}, time TBC"


def csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def csp_attr(csp: str) -> str:
    """CSP for a double-quoted attribute: keep its single quotes readable."""
    return html.escape(csp, quote=False).replace('"', "&quot;")


def js_str(value) -> str:
    """JSON for inside a <script> tag: no way to close the tag early."""
    return (json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# =====================================================================
# Static assets (kept as plain strings: no Python formatting inside)
# =====================================================================

FONTS_URL = ("https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900"
             "&family=Bricolage+Grotesque:opsz,wght@12..96,400..800"
             "&family=IBM+Plex+Mono:wght@400;700&family=Newsreader:opsz,wght@6..72,600&display=swap")

# Runs in <head> before first paint, so dark-mode readers never get a
# flash of the light theme.
THEME_BOOT_JS = ("(function(){var t=null;try{t=localStorage.getItem('theme');}catch(e){}"
                 "if(t!=='light'&&t!=='dark'){t=window.matchMedia&&matchMedia('(prefers-color-scheme: dark)')"
                 ".matches?'dark':'light';}document.documentElement.setAttribute('data-theme',t);})();")

THEME_TOKENS_LIGHT = """
    --bg: #EEF2F7; --card: #FFFFFF; --ink: #0B1F3A; --muted: #4F5F74; --line: #D8E1EC;
    --accent: #0353A4; --on-accent: #FFFFFF; color-scheme: light;"""
THEME_TOKENS_DARK = """
    --bg: #0A0A0A; --card: #171717; --ink: #F2F0E8; --muted: #A3A08F; --line: #2C2A24;
    --accent: #F5C518; --on-accent: #14110A; color-scheme: dark;"""

BASE_CSS = """
  :root {""" + THEME_TOKENS_LIGHT + """
    --win: #1E7B34; --draw: #5F6773; --loss: #C62828;
    --sans: "Archivo", system-ui, -apple-system, "Segoe UI", sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --serif: "Newsreader", Georgia, "Times New Roman", serif;
    --brand: "Bricolage Grotesque", "Archivo", system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {""" + THEME_TOKENS_DARK + """ } }
  [data-theme="dark"] {""" + THEME_TOKENS_DARK + """ }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--ink); font-family: var(--sans); line-height: 1.45; -webkit-text-size-adjust: 100%; }
  a { color: inherit; }
  a:focus-visible, button:focus-visible, summary:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
  .hidden { display: none !important; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 0 16px; }
  footer { max-width: 720px; margin: 28px auto 40px; padding: 0 16px; font-size: 13px; color: var(--muted); }
  footer p + p { margin-top: 8px; }
  footer a { color: var(--accent); }
"""

PAGE_CSS = BASE_CSS + """
  .skip { position: absolute; left: -9999px; top: 8px; z-index: 10; background: var(--card); color: var(--ink); padding: 10px 14px; border-radius: 8px; }
  .skip:focus { left: 16px; }

  .topbar { background: var(--card); border-bottom: 2px solid var(--ink); }
  .topbar-inner { max-width: 720px; margin: 0 auto; display: flex; align-items: center; gap: 4px; padding: 6px 8px 6px 16px; }
  .brand { flex: 1; min-width: 0; font-size: inherit; font-weight: inherit; }
  .brand a { display: flex; align-items: baseline; gap: 10px; text-decoration: none; min-width: 0; }
  .brand-mark { font-family: var(--brand); font-weight: 800; font-size: 22px; letter-spacing: -.01em; line-height: 44px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .iconbtn { height: 44px; width: 44px; background: none; border: none; color: var(--ink); cursor: pointer; border-radius: 8px; display: flex; align-items: center; justify-content: center; }
  /* theme button: moon in light mode (tap for dark), sun in dark mode (tap for light) */
  [data-theme="dark"] .icon-moon, :root:not([data-theme="dark"]) .icon-sun { display: none; }
  .searchbar { max-width: 720px; margin: 0 auto; padding: 0 16px 10px; }
  #search { width: 100%; height: 44px; padding: 0 14px; font: 16px var(--sans); color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 12px; }
  #search::placeholder { color: var(--muted); }

  .officialbar { max-width: 720px; margin: 0 auto; padding: 4px 16px 0; display: flex; flex-wrap: wrap; column-gap: 4px; font-family: var(--brand); font-size: 13.5px; font-weight: 600; }
  .officialbar a { color: var(--muted); text-decoration: none; text-transform: lowercase; padding: 8px 6px; }
  .officialbar a:first-child { padding-left: 0; }
  .officialbar a:hover { color: var(--accent); text-decoration: underline; }

  .update-banner { width: fit-content; max-width: calc(100% - 32px); margin: 0 auto; background: var(--ink); color: var(--bg); border-radius: 999px; padding: 6px 6px 6px 16px; display: flex; align-items: center; gap: 10px; font-family: var(--mono); font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.2); opacity: 0; visibility: hidden; height: 0; margin-top: 0; padding-top: 0; padding-bottom: 0; overflow: hidden; }
  .update-banner.show { opacity: 1; visibility: visible; height: auto; margin-top: 10px; padding-top: 6px; padding-bottom: 6px; }
  .update-banner button { background: var(--accent); color: var(--on-accent); border: none; border-radius: 999px; min-height: 36px; padding: 0 14px; font: 700 13px var(--sans); cursor: pointer; }

  .matchbar { max-width: 720px; margin: 0 auto; padding: 0 16px; }
  .mrow { display: flex; gap: 12px; align-items: baseline; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .mlabel { font-family: var(--mono); font-size: 12px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--accent); width: 44px; flex-shrink: 0; }
  .mrow.live .mlabel { color: var(--loss); }
  .mtext { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .mmain { font-size: 16px; font-weight: 700; }
  .msub { font-size: 13.5px; color: var(--muted); }
  .mrow.formrow { align-items: center; flex-wrap: wrap; row-gap: 6px; }
  .form { display: flex; gap: 5px; list-style: none; padding: 0; }
  .fr { width: 24px; height: 24px; border-radius: 5px; display: flex; align-items: center; justify-content: center; font-size: 12.5px; font-weight: 800; color: #FFFFFF; }
  .fr-W { background: var(--win); } .fr-D { background: var(--draw); } .fr-L { background: var(--loss); }
  .form-last { font-size: 13px; color: var(--muted); }

  .filters { display: flex; gap: 8px; overflow-x: auto; padding: 12px 0 4px; scrollbar-width: none; }
  .filters::-webkit-scrollbar { display: none; }
  .chip { min-height: 40px; flex-shrink: 0; padding: 0 16px; border-radius: 999px; border: 1px solid var(--line); background: var(--card); color: var(--ink); font: 600 13.5px var(--sans); cursor: pointer; }
  .chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--on-accent); font-weight: 700; }

  .more { margin-top: 4px; }
  .more summary { display: inline-flex; align-items: center; min-height: 40px; font-family: var(--mono); font-size: 12.5px; color: var(--muted); cursor: pointer; }
  .more summary:hover { color: var(--accent); }
  .more-body { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-top: 4px; }
  .more-body h2 { font: 700 12px var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--accent); }
  .more-body h2 + p { font-size: 13px; color: var(--muted); margin: 4px 0 8px; }
  .more-section + .more-section { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
  .srcchips { display: flex; gap: 6px; flex-wrap: wrap; }
  .srcchip { min-height: 34px; font: 12.5px var(--mono); border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 999px; padding: 0 12px; cursor: pointer; }
  .srcchip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
  .linkbtn { background: none; border: none; padding: 0; color: var(--accent); text-decoration: underline; font: inherit; cursor: pointer; min-height: 24px; }
  #mute-input { width: 100%; height: 44px; padding: 0 12px; font: 16px var(--sans); color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 10px; }

  .view-note, .newcount { font-size: 13px; color: var(--muted); margin-top: 8px; }
  .newcount::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); margin-right: 7px; vertical-align: 1px; }

  .lead { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 16px; margin-top: 12px; }
  .lead-kicker { font: 700 12px var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--accent); }
  .lead-headline { display: block; margin-top: 6px; font: 600 28px/1.12 var(--serif); color: var(--ink); text-decoration: none; }
  .lead-excerpt { font-size: 15px; color: var(--muted); margin-top: 8px; }
  .lead-meta, .card-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
  .lead-meta { margin-top: 8px; font-size: 13.5px; }
  .lead-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 16px; }
  .lead-sub { border-top: 1px solid var(--ink); padding-top: 10px; }
  .lead-sub .lead-meta { margin-top: 0; font-size: 12.5px; }
  .lead-sub .lead-headline { margin-top: 4px; font-size: 17.5px; line-height: 1.2; }
  .lead-headline:hover, .headline:hover { text-decoration: underline; text-underline-offset: 3px; }

  .dayhead { display: flex; justify-content: space-between; align-items: baseline; margin: 22px 0 0; }
  .day { font-weight: 900; font-stretch: 110%; font-size: 15px; letter-spacing: .1em; text-transform: uppercase; color: var(--accent); }

  .card { display: block; background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-top: 8px; }
  body:not(.lead-off) .card[data-lead="1"] { display: none; }
  .card-row { display: flex; gap: 12px; align-items: flex-start; }
  .card-body { flex: 1; min-width: 0; }
  .hl { font: inherit; letter-spacing: inherit; }
  .src { font-weight: 700; color: var(--accent); }
  .card-meta .tag { margin-left: auto; font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; }
  .badge-official { font-weight: 800; font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--accent); border: 1.5px solid var(--accent); border-radius: 4px; padding: 0 5px; }
  .badge-new { display: none; font-weight: 800; font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--on-accent); background: var(--accent); border-radius: 4px; padding: 1px 6px; }
  .is-new .badge-new { display: inline-block; }
  .headline { display: block; margin-top: 4px; font-weight: 700; font-stretch: 94%; font-size: 17.5px; line-height: 1.28; color: var(--ink); text-decoration: none; }
  .excerpt { font-size: 14.5px; line-height: 1.45; color: var(--muted); margin-top: 4px; }
  .thumb { margin-top: 6px; width: 72px; height: 72px; object-fit: cover; border-radius: 8px; flex-shrink: 0; background: var(--line); }

  @media (max-width: 359px) { .lead-grid { grid-template-columns: 1fr; } .lead-headline { font-size: 24px; } }
  @media (min-width: 640px) { .lead-headline { font-size: 32px; } }
  @media (prefers-reduced-motion: no-preference) {
    .update-banner { transition: opacity .35s ease, visibility .35s; }
    .card { transition: border-color .15s; }
    .card:hover { border-color: var(--accent); }
  }
"""

STATUS_CSS = BASE_CSS + """
  h1 { font: 700 22px var(--mono); padding: 18px 0 4px; }
  h2 { font: 700 13px var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--accent); margin: 22px 0 8px; }
  p.lede { color: var(--muted); font-size: 14px; }
  table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; font-size: 14px; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font: 700 11.5px var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
  .ok { color: #1E7B34; font-weight: 700; } .bad { color: #C62828; font-weight: 700; }
  @media (prefers-color-scheme: dark) { .ok { color: #5BD07F; } .bad { color: #FF7A7A; } }
  .num { font-variant-numeric: tabular-nums; }
  .err { color: var(--muted); font-size: 12.5px; word-break: break-word; }
  .tablewrap { overflow-x: auto; }
"""

PAGE_JS = r"""
(function () {
  'use strict';
  var doc = document, root = doc.documentElement, body = doc.body;

  // ---- storage that never throws (private mode, blocked cookies...) ----
  function store(kind) {
    var s = null;
    try { s = window[kind]; s.setItem('__twt', '1'); s.removeItem('__twt'); } catch (e) { s = null; }
    return {
      get: function (k) { try { return s ? s.getItem(k) : null; } catch (e) { return null; } },
      set: function (k, v) { try { if (s) s.setItem(k, v); } catch (e) {} },
      del: function (k) { try { if (s) s.removeItem(k); } catch (e) {} }
    };
  }
  var local = store('localStorage'), session = store('sessionStorage');
  function readList(key) {
    try { var v = JSON.parse(local.get(key) || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; }
  }
  function $(id) { return doc.getElementById(id); }
  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  // ---- theme (the starting theme is set in <head> before first paint) ----
  var toggle = $('theme-toggle');
  function themeLabel() {
    // the sun/moon icon swaps in CSS; only the accessible name changes here
    var label = root.getAttribute('data-theme') === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', label);
  }
  themeLabel();
  toggle.addEventListener('click', function () {
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    local.set('theme', next);
    themeLabel();
  });

  // ---- tidy the address bar after a cache-busting reload ----
  var cameFromRefresh = /[?&]refresh=/.test(location.search);
  if (cameFromRefresh && history.replaceState) {
    try { history.replaceState(null, '', location.pathname + location.hash); } catch (e) {}
  }

  // ---- filters, search, sources, muted words ----
  var cards = Array.prototype.slice.call(doc.querySelectorAll('.card'));
  var texts = cards.map(function (c) {
    var ex = c.querySelector('.excerpt'), hl = c.querySelector('.headline');
    return [hl ? hl.textContent : '', ex ? ex.textContent : '', c.getAttribute('data-source'),
            c.getAttribute('data-tag')].join(' ').toLowerCase();
  });
  var leadCards = cards.filter(function (c) { return c.getAttribute('data-lead') === '1'; });
  var days = Array.prototype.slice.call(doc.querySelectorAll('.dayhead'));
  var lead = $('lead');
  var chips = doc.querySelectorAll('.chip'), srcchips = doc.querySelectorAll('.srcchip');
  var search = $('search'), searchbar = $('searchbar'), searchToggle = $('search-toggle');
  var viewNote = $('view-note'), viewNoteText = $('view-note-text'), viewNoteBtn = $('view-note-btn');
  var srcNote = $('src-note'), muteInput = $('mute-input'), muteNote = $('mute-note'), more = $('more');
  var activeFilter = 'all';
  var selected = new Set(readList('selectedSources').filter(function (s) { return typeof s === 'string'; }));
  var muted = cleanWords(readList('mutedWords'));
  var muteRe = null;

  function cleanWords(list) {
    var out = [];
    each(list, function (w) {
      if (typeof w !== 'string') return;
      w = w.trim().toLowerCase().slice(0, 40);
      if (w && out.indexOf(w) === -1 && out.length < 25) out.push(w);
    });
    return out;
  }
  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function buildMuteRe() {
    muteRe = null;
    if (!muted.length) return;
    try {
      muteRe = new RegExp('(^|[^\\p{L}\\p{N}])(' + muted.map(escapeRe).join('|') + ')(?=$|[^\\p{L}\\p{N}])', 'iu');
    } catch (e) {
      muteRe = new RegExp('(' + muted.map(escapeRe).join('|') + ')', 'i');
    }
  }
  buildMuteRe();
  muteInput.value = muted.join(', ');

  var ukDayFmt = null;
  try { ukDayFmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit' }); } catch (e) {}
  function ukDay(d) { return ukDayFmt ? ukDayFmt.format(d) : d.toISOString().slice(0, 10); }

  function relabelDays(leadShown) {
    var today = ukDay(new Date()), yesterday = ukDay(new Date(Date.now() - 864e5));
    each(days, function (day) {
      var date = day.getAttribute('data-date');
      var label = date === today ? (leadShown ? 'Earlier today' : 'Today')
                : date === yesterday ? 'Yesterday' : day.getAttribute('data-long');
      var h = day.querySelector('.day');
      if (h.textContent !== label) h.textContent = label;
    });
  }

  function applyView() {
    var q = search.value.trim().toLowerCase();
    var shown = 0, mutedHidden = 0;
    each(cards, function (c, i) {
      var show = selected.size === 0 || selected.has(c.getAttribute('data-source'));
      var text = texts[i];
      if (show && activeFilter === 'official') show = c.getAttribute('data-official') === '1';
      else if (show && activeFilter.indexOf('tag:') === 0) show = c.getAttribute('data-tag') === activeFilter.slice(4);
      else if (show && activeFilter.indexOf('group:') === 0) show = c.getAttribute('data-group') === activeFilter.slice(6);
      if (show && q) show = text.indexOf(q) !== -1;
      if (show && muteRe && muteRe.test(text)) { show = false; mutedHidden++; }
      c.classList.toggle('hidden', !show);
      if (show) shown++;
    });
    // The "Latest" block shows the newest stories in the everyday view.
    // Filter, search or mute one of them and it steps aside: those stories
    // then appear as ordinary cards, so nothing is hidden or shown twice.
    var leadShown = !!lead && activeFilter === 'all' && !q && selected.size === 0 &&
      leadCards.every(function (c) { return !c.classList.contains('hidden'); });
    if (lead) lead.hidden = !leadShown;
    body.classList.toggle('lead-off', !leadShown);
    each(days, function (day) {
      var el = day.nextElementSibling, any = false;
      while (el && !el.classList.contains('dayhead')) {
        if (el.classList.contains('card') && !el.classList.contains('hidden') &&
            !(leadShown && el.getAttribute('data-lead') === '1')) { any = true; break; }
        el = el.nextElementSibling;
      }
      day.classList.toggle('hidden', !any);
    });
    relabelDays(leadShown);
    if (shown === 0) {
      viewNote.hidden = false;
      viewNoteText.textContent = 'No headlines match.';
      viewNoteBtn.textContent = 'Clear filters';
      viewNoteBtn.setAttribute('data-action', 'clear');
    } else if (mutedHidden) {
      viewNote.hidden = false;
      viewNoteText.textContent = mutedHidden + (mutedHidden === 1 ? ' headline' : ' headlines') + ' hidden by your muted words.';
      viewNoteBtn.textContent = 'Edit';
      viewNoteBtn.setAttribute('data-action', 'mute');
    } else {
      viewNote.hidden = true;
    }
  }

  function setFilter(filter) {
    activeFilter = filter;
    each(chips, function (c) { c.setAttribute('aria-pressed', c.getAttribute('data-filter') === filter ? 'true' : 'false'); });
  }
  each(chips, function (chip) {
    chip.addEventListener('click', function () { setFilter(chip.getAttribute('data-filter')); applyView(); });
  });

  function saveSources() { local.set('selectedSources', JSON.stringify(Array.from(selected))); }
  function syncSrcChips() {
    each(srcchips, function (c) { c.setAttribute('aria-pressed', selected.has(c.getAttribute('data-src')) ? 'true' : 'false'); });
    srcNote.textContent = selected.size ? '(' + selected.size + ' selected)' : '';
  }
  each(srcchips, function (chip) {
    chip.addEventListener('click', function () {
      var s = chip.getAttribute('data-src');
      if (selected.has(s)) selected.delete(s); else selected.add(s);
      saveSources(); syncSrcChips(); applyView();
    });
  });
  $('src-clear').addEventListener('click', function () { selected = new Set(); saveSources(); syncSrcChips(); applyView(); });

  var muteTimer = null;
  function saveMutes() {
    muted = cleanWords(muteInput.value.split(','));
    local.set('mutedWords', JSON.stringify(muted));
    buildMuteRe();
    muteNote.textContent = muted.length ? 'Hiding headlines containing: ' + muted.join(', ') : '';
    applyView();
  }
  muteNote.textContent = muted.length ? 'Hiding headlines containing: ' + muted.join(', ') : '';
  muteInput.addEventListener('input', function () { clearTimeout(muteTimer); muteTimer = setTimeout(saveMutes, 400); });
  muteInput.addEventListener('change', saveMutes);

  viewNoteBtn.addEventListener('click', function () {
    if (viewNoteBtn.getAttribute('data-action') === 'mute') {
      more.open = true; muteInput.focus(); return;
    }
    setFilter('all'); selected = new Set(); saveSources(); syncSrcChips();
    search.value = ''; applyView();
  });

  function setSearchOpen(open) {
    searchbar.hidden = !open;
    searchToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) { search.focus(); }
    else if (search.value) { search.value = ''; applyView(); }
  }
  searchToggle.addEventListener('click', function () { setSearchOpen(searchbar.hidden); });
  search.addEventListener('input', applyView);
  search.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { setSearchOpen(false); searchToggle.focus(); }
  });

  // ---- "new since your last visit" ----
  // Remembers the newest story you were shown (not the clock), so a
  // wrong device clock can't hide or invent new stories.
  var NEWEST = body.getAttribute('data-latest-published') || '';
  var lastSeen = local.get('twtLastSeen');
  var reloading = false;
  if (lastSeen) {
    var fresh = 0;
    each(cards, function (c) {
      if ((c.getAttribute('data-published') || '') > lastSeen) { c.classList.add('is-new'); fresh++; }
    });
    each(doc.querySelectorAll('.lead-item'), function (el) {
      if ((el.getAttribute('data-published') || '') > lastSeen) el.classList.add('is-new');
    });
    if (fresh) {
      var nc = $('newcount');
      nc.textContent = fresh + ' new since your last visit';
      nc.hidden = false;
    }
  }
  function remember() { if (NEWEST && !reloading) local.set('twtLastSeen', NEWEST); }
  doc.addEventListener('visibilitychange', function () { if (doc.visibilityState === 'hidden') remember(); });
  window.addEventListener('pagehide', remember);

  // a picture the publisher has since removed shouldn't leave a broken box
  each(doc.querySelectorAll('img.thumb'), function (img) {
    if (img.complete && !img.naturalWidth) img.remove();
    else img.addEventListener('error', function () { img.remove(); });
  });

  syncSrcChips();
  applyView();

  // ---- live relative times and the kick-off countdown ----
  function relTime(iso) {
    var mins = Math.max(1, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
    if (mins < 60) return mins + 'm ago';
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.floor(hours / 24) + 'd ago';
  }
  function tick() {
    each(doc.querySelectorAll('.time[data-published]'), function (el) {
      var t = relTime(el.getAttribute('data-published'));
      if (el.textContent !== t) el.textContent = t;
    });
    each(doc.querySelectorAll('[data-kickoff]'), function (el) {
      var ms = new Date(el.getAttribute('data-kickoff')).getTime() - Date.now();
      if (isNaN(ms)) return;
      var mins = Math.floor(ms / 60000), d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
      el.textContent = ms <= 0 ? 'kicked off' : 'in ' + (d ? d + 'd ' + h + 'h' : h ? h + 'h ' + m + 'm' : Math.max(m, 1) + 'm');
    });
  }
  tick();
  setInterval(tick, 60000);

  // ---- check for new content ----
  // Compares the newest story, not the build time: the site rebuilds every
  // ~15 minutes whether or not anything new was found.
  var PAGE_LATEST = body.getAttribute('data-latest') || null;
  var banner = $('update-banner');
  function freshUrl() { return location.pathname + '?refresh=' + Date.now(); }
  function checkForUpdate(initial) {
    if (!window.fetch) return;
    fetch('version.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.latest) return;
        var newer = data.latest !== PAGE_LATEST &&
          (!data.latest_published || !NEWEST || data.latest_published > NEWEST);
        if (newer) {
          // The HTML itself may have come from a cache. Quietly reload once;
          // the ?refresh marker and the session flag stop any reload loop
          // (the marker works even when storage is blocked).
          if (initial && !cameFromRefresh && !session.get('twtReloaded')) {
            session.set('twtReloaded', '1');
            reloading = true;
            location.replace(freshUrl());
            return;
          }
          banner.classList.add('show');
        } else if (initial) {
          session.del('twtReloaded');
        }
      })
      .catch(function () { /* offline or blocked - try again next time */ });
  }
  $('update-refresh').addEventListener('click', function () { location.href = freshUrl(); });
  checkForUpdate(true);
  setInterval(function () { checkForUpdate(false); }, 3 * 60000);
  doc.addEventListener('visibilitychange', function () { if (!doc.hidden) { checkForUpdate(false); tick(); } });
})();
"""

SEARCH_ICON = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
               'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
               '<circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>')

_ICON_ATTRS = ('width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
               'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"')
THEME_ICONS = (f'<svg class="icon-moon" {_ICON_ATTRS}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"></path></svg>'
               f'<svg class="icon-sun" {_ICON_ATTRS}><circle cx="12" cy="12" r="4"></circle>'
               '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2'
               'M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"></path></svg>')


# =====================================================================
# Rendering
# =====================================================================

def render_matchbar(fx: dict, now: datetime) -> str:
    if not isinstance(fx, dict) or fx.get("sample", True):
        return ""
    rows = []
    live, nxt = fx.get("live"), fx.get("next")
    try:
        if live and live.get("score") and parse_iso(live["date"]) + timedelta(hours=2, minutes=30) > now:
            sub = " · ".join(filter(None, [esc(live.get("competition")),
                                           f"score at {now.astimezone(UK):%H:%M}"]))
            rows.append(
                f'<div class="mrow live"><span class="mlabel">Live</span><div class="mtext">'
                f'<span class="mmain">{esc(live["home"])} {esc(live["score"])} {esc(live["away"])}</span>'
                f'<span class="msub">{sub}</span></div></div>')
        elif nxt and parse_iso(nxt["date"]) + timedelta(hours=2, minutes=30) > now:
            ko = parse_iso(nxt["date"])
            bits = [esc(nxt.get("competition")), esc(fmt_kickoff(nxt["date"], nxt.get("time_known", True)))]
            countdown = (f'<span class="countdown" data-kickoff="{esc(ko.isoformat())}"></span>'
                         if nxt.get("time_known", True) else "")
            sub = " · ".join(b for b in bits if b) + (f" · {countdown}" if countdown else "")
            rows.append(
                f'<div class="mrow"><span class="mlabel">Next</span><div class="mtext">'
                f'<span class="mmain">{esc(nxt["home"])} v {esc(nxt["away"])}</span>'
                f'<span class="msub">{sub}</span></div></div>')
    except (KeyError, ValueError, TypeError):
        pass

    try:
        form = [f for f in (fx.get("form") or []) if isinstance(f, dict) and f.get("result") in ("W", "D", "L")]
        if form:
            words = {"W": "Won", "D": "Drew", "L": "Lost"}
            items = []
            for f in form:
                ours_first = _our_score(f)
                opp = f.get("away") if f.get("is_home") else f.get("home")
                where = "at home" if f.get("is_home") else "away"
                when = ""
                if f.get("date"):
                    kd = parse_iso(f["date"]).astimezone(UK)
                    when = f"{kd.day} {kd:%b}"
                label = f"{words[f['result']]} {ours_first} v {opp} {where}, {when}".strip(", ")
                items.append(f'<li class="fr fr-{f["result"]}" title="{esc(label)}">'
                             f'<span aria-hidden="true">{f["result"]}</span><span class="sr-only">{esc(label)}</span></li>')
            latest = form[-1]
            opp = (latest.get("away_short") or latest.get("away")) if latest.get("is_home") else \
                (latest.get("home_short") or latest.get("home"))
            last_txt = f'{_our_score(latest)} {"v" if latest.get("is_home") else "at"} {opp}'
            rows.append(
                f'<div class="mrow formrow"><span class="mlabel">Form</span>'
                f'<ol class="form" aria-label="Last {len(form)} results, oldest first">{"".join(items)}</ol>'
                f'<span class="form-last">Last: {esc(last_txt)}</span></div>')
    except (KeyError, ValueError, TypeError):
        pass  # a malformed form entry just hides the row; never breaks the build

    if not rows:
        return ""
    return f'<section class="matchbar" aria-label="Fixtures and form">{"".join(rows)}</section>'


def _our_score(m: dict) -> str:
    """'2–1' with Wednesday's goals first."""
    score = str(m.get("score") or "")
    parts = score.split("\u2013")
    if len(parts) == 2 and not m.get("is_home"):
        return f"{parts[1]}\u2013{parts[0]}"
    return score


def render_meta(a: dict, now: datetime, *, show_tag: bool = True) -> str:
    bits = ['<span class="badge-new">New</span>']
    if a.get("official"):
        bits.append('<span class="badge-official">Official</span>')
    bits.append(f'<span class="src">{esc(a["source"])}</span>')
    bits.append(f'<span class="time" data-published="{esc(a["published"])}">{rel_time(a["published"], now)}</span>')
    if show_tag and a.get("tag"):
        bits.append(f'<span class="tag">{esc(a["tag"])}</span>')
    return "".join(bits)


def summary_of(a: dict) -> str:
    return a.get("description") or a.get("excerpt") or ""


def render_lead(articles: list[dict], now: datetime) -> str:
    if not articles:
        return ""
    top, rest = articles[0], articles[1:LEAD_STORIES]
    summary = summary_of(top)
    subs = "".join(
        f'<div class="lead-sub lead-item" data-published="{esc(a["published"])}">'
        f'<div class="lead-meta">{render_meta(a, now, show_tag=False)}</div>'
        f'<h3 class="hl"><a class="lead-headline" href="{esc(a["href"])}" target="_blank" rel="noopener noreferrer">{esc(a["title"])}</a></h3>'
        f'</div>'
        for a in rest)
    return (
        f'<section id="lead" class="lead" aria-labelledby="lead-kicker">'
        f'<div class="lead-item" data-published="{esc(top["published"])}">'
        f'<h2 id="lead-kicker" class="lead-kicker">Latest · '
        f'<span class="time" data-published="{esc(top["published"])}">{rel_time(top["published"], now)}</span></h2>'
        f'<h3 class="hl"><a class="lead-headline" href="{esc(top["href"])}" target="_blank" rel="noopener noreferrer">{esc(top["title"])}</a></h3>'
        + (f'<p class="lead-excerpt">{esc(summary)}</p>' if summary else "")
        + f'<div class="lead-meta">{render_meta(top, now)}</div></div>'
        + (f'<div class="lead-grid">{subs}</div>' if subs else "")
        + '</section>')


def render_cards(articles: list[dict], now: datetime) -> str:
    today = now.astimezone(UK).date()
    out, current = [], None
    lead_urls = {a["url"] for a in articles[:LEAD_STORIES]}
    # day -> does it have any story that isn't already in the Latest block?
    has_cards: dict = {}
    for a in articles:
        d = uk_date(a["published"])
        has_cards[d] = has_cards.get(d, False) or a["url"] not in lead_urls
    for a in articles:
        d = uk_date(a["published"])
        if d != current:
            label = day_label(d, today)
            if label == "Today" and lead_urls:
                label = "Earlier today"
            hidden = "" if has_cards[d] else " hidden"
            out.append(f'<div class="dayhead{hidden}" data-date="{d.isoformat()}" data-long="{esc(long_date(d))}">'
                       f'<h2 class="day">{label}</h2></div>')
            current = d
        summary = summary_of(a)
        img = safe_img(a.get("image"))
        thumb = (f'<img class="thumb" src="{esc(img)}" alt="" width="72" height="72" loading="lazy" '
                 f'decoding="async" referrerpolicy="no-referrer">') if img else ""
        out.append(
            f'<article class="card" data-source="{esc(a["source"])}" data-official="{"1" if a.get("official") else "0"}" '
            f'data-tag="{esc(a.get("tag") or "")}" data-group="{esc(source_group(a["source"]))}" '
            f'data-published="{esc(a["published"])}" data-lead="{"1" if a["url"] in lead_urls else "0"}">'
            f'<div class="card-meta">{render_meta(a, now)}</div>'
            f'<div class="card-row"><div class="card-body">'
            f'<h3 class="hl"><a class="headline" href="{esc(a["href"])}" target="_blank" rel="noopener noreferrer">{esc(a["title"])}</a></h3>'
            + (f'<p class="excerpt">{esc(summary)}</p>' if summary else "")
            + f'</div>{thumb}</div></article>')
    return "".join(out)


def render_page(articles: list[dict], fixtures: dict, now: datetime) -> str:
    sources = sorted({a["source"] for a in articles}, key=str.lower)
    src_chips = "".join(
        f'<button type="button" class="srcchip" data-src="{esc(s)}" aria-pressed="false">{esc(s)}</button>'
        for s in sources)
    used_groups = sorted({source_group(a["source"]) for a in articles if source_group(a["source"])})
    used_tags = [t for t, _ in TAG_RULES if any(a.get("tag") == t for a in articles)]
    chip_defs = ([("all", "All"), ("official", "Official")]
                 + [(f"group:{g}", g) for g in used_groups]
                 + [(f"tag:{t}", t) for t in used_tags])
    chips = "".join(
        f'<button type="button" class="chip" data-filter="{esc(f)}" aria-pressed="{"true" if f == "all" else "false"}">{esc(label)}</button>'
        for f, label in chip_defs)
    follow = "".join(f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(label)}</a>'
                     for label, url in OFFICIAL_LINKS)
    built_uk = now.astimezone(UK)
    goat = (f'<script data-goatcounter="https://{esc(GOATCOUNTER_CODE)}.goatcounter.com/count" '
            f'async src="https://gc.zgo.at/count.js"></script>') if GOATCOUNTER_CODE else ""
    ld = js_str({
        "@context": "https://schema.org", "@type": "WebSite", "name": SITE_NAME,
        "alternateName": "The Wednesday Times", "url": f"{SITE_URL}/",
        "description": "Sheffield Wednesday headlines from across the web in one clean, ad-free feed.",
        "inLanguage": "en-GB",
    })
    csp = "; ".join([
        "default-src 'self'",
        f"script-src 'self' {csp_hash(THEME_BOOT_JS)} {csp_hash(PAGE_JS)} https://gc.zgo.at https://static.cloudflareinsights.com",
        f"style-src 'self' {csp_hash(PAGE_CSS)} https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' https: data:",
        "connect-src 'self'" + (f" https://{GOATCOUNTER_CODE}.goatcounter.com" if GOATCOUNTER_CODE else "")
        + " https://cloudflareinsights.com",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "object-src 'none'",
        "upgrade-insecure-requests",
    ])
    newest = articles[0]
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp_attr(csp)}">
<title>{SITE_NAME} - Sheffield Wednesday news, no clutter</title>
<meta name="description" content="Sheffield Wednesday headlines in one clean, ad-free feed. Links go straight to the original source.">
<link rel="canonical" href="{SITE_URL}/">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{SITE_NAME} - Owls headlines, no clutter">
<meta property="og:description" content="Sheffield Wednesday news from multiple sources in one clean, ad-free feed. Free, updated every 15 minutes.">
<meta property="og:type" content="website">
<meta property="og:url" content="{SITE_URL}/">
<meta property="og:image" content="{SITE_URL}/share.png">
<meta property="og:image:alt" content="{SITE_NAME}">
<meta property="og:locale" content="en_GB">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#FFFFFF" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#171717" media="(prefers-color-scheme: dark)">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME}" href="{SITE_URL}/feed.xml">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.webmanifest">
<script>{THEME_BOOT_JS}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{esc(FONTS_URL)}" rel="stylesheet">
{goat}
<script type="application/ld+json">{ld}</script>
<style>{PAGE_CSS}</style>
</head>
<body data-latest="{esc(newest['url'])}" data-latest-published="{esc(newest['published'])}">
<a class="skip" href="#feed">Skip to headlines</a>
<header class="topbar">
  <div class="topbar-inner">
    <h1 class="brand"><a href="/"><span class="brand-mark">{SITE_NAME}</span></a></h1>
    <button type="button" id="search-toggle" class="iconbtn" aria-label="Search headlines" aria-expanded="false" aria-controls="searchbar">{SEARCH_ICON}</button>
    <button type="button" id="theme-toggle" class="iconbtn" aria-label="Switch to dark mode" title="Switch to dark mode">{THEME_ICONS}</button>
  </div>
  <div id="searchbar" class="searchbar" hidden>
    <label for="search" class="sr-only">Search headlines</label>
    <input id="search" type="search" placeholder="Search headlines" autocomplete="off" enterkeyhint="search">
  </div>
</header>
<nav class="officialbar" aria-label="Official club and league links">{follow}</nav>
<div id="update-banner" class="update-banner" role="status" aria-live="polite">
  <span>New stories available</span>
  <button type="button" id="update-refresh">Refresh</button>
</div>
{render_matchbar(fixtures, now)}
<main id="feed" class="wrap">
  <div class="filters" role="group" aria-label="Filter headlines">{chips}</div>
  <details id="more" class="more">
    <summary>Sources &amp; muted words <span id="src-note"></span></summary>
    <div class="more-body">
      <div class="more-section">
        <h2>Sources</h2>
        <p>Tap sources to show only those (tap again to remove). <button type="button" id="src-clear" class="linkbtn">Clear</button></p>
        <div class="srcchips">{src_chips}</div>
      </div>
      <div class="more-section">
        <h2><label for="mute-input">Muted words</label></h2>
        <p>Hide headlines containing any of these words. Separate with commas. Saved on this device only.</p>
        <input id="mute-input" type="text" placeholder="e.g. vardy, betting" autocomplete="off" spellcheck="false">
        <p id="mute-note" class="view-note"></p>
      </div>
    </div>
  </details>
  <p id="newcount" class="newcount" hidden></p>
  <p id="view-note" class="view-note" hidden><span id="view-note-text"></span> <button type="button" id="view-note-btn" class="linkbtn"></button></p>
  {render_lead(articles[:LEAD_STORIES], now)}
  {render_cards(articles, now)}
</main>
<footer>
  <p>Headlines link straight to the original publishers - read the full stories there.
  Updated {built_uk:%H:%M} UK time, {built_uk.day} {built_uk:%b %Y}.</p>
  <p>Independent and unofficial - not affiliated with Sheffield Wednesday FC or the EFL.
  Free and ad-free. If it's useful, <a href="{esc(KOFI_URL)}" target="_blank" rel="noopener noreferrer">you can support it here</a>.</p>
  <p><a href="status.html">Feed status</a> · <a href="feed.xml">RSS feed</a></p>
</footer>
<script>{PAGE_JS}</script>
</body>
</html>
"""


def render_status(status: dict, articles: list[dict], now: datetime) -> str:
    built_uk = now.astimezone(UK)
    feeds = status.get("feeds") if isinstance(status, dict) else None
    rows = []
    for f in feeds or []:
        if not isinstance(f, dict):
            continue
        ok = bool(f.get("ok"))
        result = '<span class="ok">OK</span>' if ok else '<span class="bad">Failed</span>'
        err = f'<div class="err">{esc(f.get("error"))}</div>' if f.get("error") else ""
        rows.append(f'<tr><td>{esc(f.get("source"))}<div class="err">{esc(f.get("host"))}</div></td>'
                    f'<td>{result}{err}</td><td class="num">{esc(f.get("entries", 0))}</td>'
                    f'<td class="num">{esc(f.get("kept", 0))}</td></tr>')
    feed_table = ('<div class="tablewrap"><table><thead><tr><th>Feed</th><th>Last run</th><th>Stories</th>'
                  '<th>Kept</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>') if rows else \
        '<p class="lede">No feed report from the last run.</p>'

    filtered = status.get("filtered") if isinstance(status, dict) else None
    frows = "".join(
        f'<tr><td>{esc(FILTER_REASONS.get(k, k))}</td><td class="num">{esc(v)}</td></tr>'
        for k, v in sorted((filtered or {}).items(), key=lambda kv: -int(kv[1] or 0)) if v)
    filter_table = ('<div class="tablewrap"><table><thead><tr><th>Left out because</th><th>Stories</th></tr></thead>'
                    f'<tbody>{frows}</tbody></table></div>') if frows else '<p class="lede">Nothing was filtered out.</p>'

    fx = status.get("fixtures") if isinstance(status, dict) else None
    if isinstance(fx, dict):
        fx_line = ("Fixtures and results: <span class=\"ok\">OK</span>" if fx.get("ok")
                   else f'Fixtures and results: <span class="bad">Failed</span> - {esc(fx.get("error"))}')
    else:
        fx_line = "Fixtures and results: no report from the last run."
    counts = status.get("articles") if isinstance(status, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    n_sources = len({a["source"] for a in articles})
    style_hash = csp_hash(STATUS_CSS)
    csp = f"default-src 'none'; style-src {style_hash}; img-src 'self'; base-uri 'none'; form-action 'none'"
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp_attr(csp)}">
<meta name="robots" content="noindex">
<title>Feed status - {SITE_NAME}</title>
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<style>{STATUS_CSS}</style>
</head>
<body>
<main class="wrap">
  <h1>Feed status</h1>
  <p class="lede">How the last update went. Built {built_uk:%H:%M} UK time, {built_uk.day} {built_uk:%b %Y}.
  {len(articles)} stories from {n_sources} sources on the site
  ({esc(counts.get("fresh", "?"))} found this run, {esc(counts.get("carried_over", "?"))} carried over from the last run).</p>
  <h2>Feeds</h2>
  {feed_table}
  <p class="lede">{fx_line}</p>
  <h2>Filtered out this run</h2>
  <p class="lede">The site shows everything the feeds send, newest first, except these.
  Nothing is ranked or hidden for any other reason.</p>
  {filter_table}
</main>
<footer><p><a href="./">Back to the headlines</a></p></footer>
</body>
</html>
"""


def render_feed_xml(articles: list[dict], now: datetime) -> str:
    items = []
    for a in articles[:40]:
        summary = summary_of(a)
        desc = f"{summary} ({a['source']})" if summary else a["source"]
        items.append(f"""
  <item>
    <title>{esc(a['title'])}</title>
    <link>{esc(a['href'])}</link>
    <guid isPermaLink="true">{esc(a['href'])}</guid>
    <pubDate>{format_datetime(parse_iso(a['published']).astimezone(timezone.utc), usegmt=True)}</pubDate>
    <source url="{SITE_URL}/feed.xml">{esc(a['source'])}</source>
    <description>{esc(desc)}</description>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{SITE_NAME}</title>
  <link>{SITE_URL}/</link>
  <atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>
  <description>Sheffield Wednesday headlines in one clean feed. Links go to the original publishers.</description>
  <language>en-gb</language>
  <lastBuildDate>{format_datetime(now, usegmt=True)}</lastBuildDate>{"".join(items)}
</channel>
</rss>
"""


def render_sitemap(now: datetime) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{now:%Y-%m-%d}</lastmod>
    <changefreq>hourly</changefreq>
  </url>
</urlset>
"""


def render_manifest() -> str:
    return json.dumps({
        "name": SITE_NAME,
        "short_name": "Wed Wire",
        "description": "Sheffield Wednesday headlines in one clean, ad-free feed.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#16233B",
        "theme_color": "#16233B",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, indent=2)


# =====================================================================
# Main
# =====================================================================

def prepare(raw) -> list[dict]:
    """Keep only well-formed articles with safe links, newest first."""
    out = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        href = safe_href(a.get("url"))
        title = str(a.get("title") or "").strip()
        try:
            parse_iso(a.get("published"))
        except (TypeError, ValueError):
            continue
        if not href or not title or not a.get("source"):
            continue
        a = dict(a, href=href, title=title, source=str(a["source"]))
        a["tag"] = tag_for(a)
        out.append(a)
    out.sort(key=lambda a: parse_iso(a["published"]), reverse=True)
    return out


def main(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    try:
        raw = json.loads((HERE / "articles.json").read_text())
    except (FileNotFoundError, ValueError) as e:
        print(f"::error::articles.json is missing or unreadable ({e}) - not building.")
        return 1
    articles = prepare(raw)
    if len(articles) < MIN_ARTICLES:
        print(f"::error::Only {len(articles)} usable articles (need {MIN_ARTICLES}) - "
              "not building, so the live site keeps its last good version.")
        return 1
    fixtures = load_json(HERE / "fixtures.json", {})
    status = load_json(HERE / "status.json", {})

    page = render_page(articles, fixtures, now)
    if page.count('<article class="card"') != len(articles):
        print("::error::Rendered page is missing stories - not building.")
        return 1

    outputs = {
        "index.html": page,
        "status.html": render_status(status, articles, now),
        "feed.xml": render_feed_xml(articles, now),
        "sitemap.xml": render_sitemap(now),
        "robots.txt": f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n",
        "manifest.webmanifest": render_manifest(),
        # tiny version marker, polled client-side to detect new content
        "version.json": json.dumps({"built": now.isoformat(timespec="seconds"),
                                    "latest": articles[0]["url"],
                                    "latest_published": articles[0]["published"]}),
    }
    for name, text in outputs.items():
        write_atomic(HERE / name, text)

    n_official = sum(1 for a in articles if a.get("official"))
    tags = sorted({a["tag"] for a in articles if a["tag"]})
    print(f"Built index.html + status.html + feed.xml + sitemap.xml: {len(articles)} articles "
          f"({n_official} official), tags: {', '.join(tags) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
