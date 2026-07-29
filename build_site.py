#!/usr/bin/env python3
"""
The Wednesday Times — site builder.

Renders articles.json into index.html: a clean, fast, ad-free static page
with light/dark mode and an official-sources filter. Run after
fetch_news.py. Deploy anywhere static files go (GitHub Pages is free).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
ARTICLES = json.loads((HERE / "articles.json").read_text())
BUILT_AT = datetime.now(timezone.utc)
BUILT_AT_UK = BUILT_AT.astimezone(ZoneInfo("Europe/London"))

# Set this to your real URL once deployed (needed for social link previews
# and the RSS feed) e.g. "https://yourusername.github.io/all-wednesday"
SITE_URL = "https://allwednesday.uk"

# Free, cookie-free analytics: https://www.goatcounter.com/ (no signup cost).
# Sign up, then put your code here (the bit before ".goatcounter.com").
# Leave blank to skip analytics entirely — nothing breaks either way.
GOATCOUNTER_CODE = "allwednesday"

try:
    FIXTURES = json.loads((HERE / "fixtures.json").read_text())
except FileNotFoundError:
    FIXTURES = {}

# ---- story tagging (keyword-based, tune freely) ----
TAG_RULES = [
    ("Transfers", ["transfer", "sign", "signing", "loan", "linked", "bid", "deal", "contract", "fee", "swoop", "target"]),
    ("Injuries", ["injur", "knock", "fitness", "ruled out", "sidelined", "scan", "hamstring", "surgery", "return date"]),
    ("Match", ["highlights", "report", "full-time", "friendly", "kick-off", "line-up", "lineup", "reaction", "player ratings", "preview", "team news"]),
    ("Youth", ["academy", "under-21", "under-18", "u21", "u18", "youth", "development squad"]),
    ("Club news", ["statement", "ticket", "announce", "confirm", "hillsborough", "ownership", "takeover", "efl"]),
]


def tag_for(article: dict) -> str | None:
    text = (article["title"] + " " + article.get("excerpt", "")).lower()
    for tag, kws in TAG_RULES:
        if any(kw in text for kw in kws):
            return tag
    return None

# Official accounts for the follow bar — links only, no embeds needed.
OFFICIAL_LINKS = [
    ("Club site", "https://www.swfc.co.uk"),
    ("X", "https://x.com/swfc"),
    ("Instagram", "https://www.instagram.com/swfcofficial"),
    ("YouTube", "https://www.youtube.com/user/officialswfc"),
    ("Facebook", "https://www.facebook.com/sheffieldwednesday"),
    ("EFL", "https://www.efl.com"),
]


def rel_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    mins = int((BUILT_AT - dt).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins,1)}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def day_bucket(iso: str) -> str:
    d = datetime.fromisoformat(iso).date()
    today = BUILT_AT.date()
    if d == today:
        return "Today"
    if (today - d).days == 1:
        return "Yesterday"
    return d.strftime("%A %d %B")


sources = sorted({a["source"] for a in ARTICLES})
chips = "".join(
    f'<button class="srcchip" data-src="{html.escape(s)}" aria-pressed="true">{html.escape(s)}</button>'
    for s in sources
)

for a in ARTICLES:
    a["tag"] = tag_for(a)
used_tags = sorted({a["tag"] for a in ARTICLES if a["tag"]})
tag_chips = "".join(
    f'<button class="chip" data-filter="tag:{html.escape(t)}">{html.escape(t)}</button>'
    for t in used_tags
)

follow = "".join(
    f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>'
    for label, url in OFFICIAL_LINKS
)

# ---- fixtures strip ----
def fmt_kickoff(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.strftime("%a %d %b, %H:%M")

fix_bits = []
nxt = FIXTURES.get("next")
if nxt:
    fix_bits.append(
        f'<div class="fix"><span class="fix-label">Next</span>'
        f'{html.escape(nxt["home"])} v {html.escape(nxt["away"])}'
        f'<span class="fix-sub">{fmt_kickoff(nxt["date"])}</span></div>'
    )
last = FIXTURES.get("last")
if last and last.get("score"):
    fix_bits.append(
        f'<div class="fix"><span class="fix-label">Last</span>'
        f'{html.escape(last["home"])} {html.escape(last["score"])} {html.escape(last["away"])}</div>'
    )
st = FIXTURES.get("standing")
if st:
    fix_bits.append(
        f'<div class="fix"><span class="fix-label">League</span>'
        f'{st["position"]}th · P{st["played"]} · {st["points"]}pts</div>'
    )
fixtures_html = (
    f'<div class="fixbar"><div class="inner">{"".join(fix_bits)}</div></div>'
    if fix_bits and not FIXTURES.get("sample", True)
    else ""
)

items_html = ""
current_bucket = None
for a in ARTICLES:
    b = day_bucket(a["published"])
    if b != current_bucket:
        items_html += f'<h2 class="day">{b}</h2>'
        current_bucket = b
    badge = '<span class="badge">Official</span>' if a.get("official") else ""
    tagpill = f'<span class="tagpill">{html.escape(a["tag"])}</span>' if a.get("tag") else ""
    items_html += f"""
    <article class="item{' official' if a.get('official') else ''}" data-source="{html.escape(a['source'])}" data-official="{'1' if a.get('official') else '0'}" data-tag="{html.escape(a.get('tag') or '')}">
      <div class="meta"><span class="time" data-published="{html.escape(a['published'])}">{rel_time(a['published'])}</span><span class="src">{html.escape(a['source'])}</span>{badge}{tagpill}</div>
      <a class="headline" href="{html.escape(a['url'])}" target="_blank" rel="noopener">{html.escape(a['title'])}</a>
      {f'<p class="excerpt">{html.escape(a["excerpt"])}</p>' if a.get('excerpt') else ''}
    </article>"""

page = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Wednesday Times — Sheffield Wednesday news, no clutter</title>
<meta name="description" content="Sheffield Wednesday headlines in one clean, ad-free feed. Links go straight to the original source.">
<meta property="og:title" content="The Wednesday Times — Owls headlines, no clutter">
<meta property="og:description" content="Sheffield Wednesday news from multiple sources in one clean, ad-free feed. Free, updated every 30 minutes.">
<meta property="og:type" content="website">
<meta property="og:url" content="{SITE_URL}/">
<meta property="og:image" content="{SITE_URL}/share.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="alternate" type="application/rss+xml" title="The Wednesday Times" href="{SITE_URL}/feed.xml">
<link rel="icon" href="favicon.png" type="image/png">
{f'<script data-goatcounter="https://{GOATCOUNTER_CODE}.goatcounter.com/count" async src="https://gc.zgo.at/count.js"></script>' if GOATCOUNTER_CODE else ''}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&family=Source+Sans+3:wght@400;600&family=Space+Grotesk:wght@500&display=swap" rel="stylesheet">
<style>
  :root {{
    --blue: #0353A4;
    --blue-soft: #7FB2E5;
    --ink: #0B1F3A;
    --bg: #F4F7FB;
    --card: #FFFFFF;
    --line: #D8E1EC;
    --muted: #5B6B80;
    --head-bg: #0B1F3A;
    --head-fg: #FFFFFF;
    --stripe-a: #0353A4;
    --stripe-b: #FFFFFF;
    --badge-fg: #FFFFFF;
  }}
  [data-theme="dark"] {{
    --blue: #F5C518;
    --blue-soft: #FFDE70;
    --ink: #F2F0E8;
    --bg: #0A0A0A;
    --card: #171717;
    --line: #2C2A24;
    --muted: #A3A08F;
    --head-bg: #0D0D0D;
    --head-fg: #F2F0E8;
    --stripe-a: #F5C518;
    --stripe-b: #0A0A0A;
    --badge-fg: #14110A;
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ background: var(--bg); color: var(--ink); font-family: "Source Sans 3", sans-serif; line-height: 1.5; }}

  header {{ background: var(--card); color: var(--ink); border-bottom: 3px solid var(--ink); }}
  .mast {{ max-width: 720px; margin: 0 auto; padding: 1rem 1.2rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
  .wordmark {{ text-align: center; line-height: 1; }}
  .wm-rule {{ border-top: 1.5px solid var(--ink); margin: .25rem auto; max-width: 340px; }}
  .wm-the, .wm-times {{ font-family: Georgia, "Times New Roman", serif; font-style: italic; font-size: .8rem; color: var(--ink); margin: .1rem 0; }}
  .wm-wednesday {{ font-family: "Archivo", sans-serif; font-variation-settings: "wdth" 115; font-weight: 900; font-size: clamp(1.7rem, 6vw, 2.5rem); letter-spacing: .01em; text-transform: uppercase; color: var(--ink); margin: .05rem 0; }}
  .mast-right {{ display: flex; align-items: center; gap: .9rem; }}
  .tag {{ font-family: "Space Grotesk", monospace; font-size: .78rem; color: var(--muted); }}
  #theme-toggle {{ background: transparent; border: 1px solid var(--ink); color: var(--ink); border-radius: 999px; font-family: "Space Grotesk", monospace; font-size: .75rem; padding: .3rem .75rem; cursor: pointer; }}
  #theme-toggle:focus-visible {{ outline: 2px solid var(--ink); outline-offset: 2px; }}

  .followbar {{ background: var(--bg); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
  .followbar .inner {{ max-width: 720px; margin: 0 auto; padding: .5rem 1.2rem .8rem; display: flex; gap: 1rem; flex-wrap: wrap; font-family: "Space Grotesk", monospace; font-size: .74rem; }}
  .followbar span {{ color: var(--muted); }}
  .followbar a {{ color: var(--blue); text-decoration: none; }}
  .followbar a:hover {{ text-decoration: underline; }}

  .fixbar {{ background: var(--card); border-bottom: 1px solid var(--line); }}
  .fixbar .inner {{ max-width: 720px; margin: 0 auto; padding: .55rem 1.2rem; display: flex; gap: 1.4rem; flex-wrap: wrap; font-family: "Space Grotesk", monospace; font-size: .78rem; color: var(--ink); }}
  .fix-label {{ color: var(--blue); text-transform: uppercase; font-size: .65rem; letter-spacing: .1em; margin-right: .45rem; }}
  .fix-sub {{ color: var(--muted); margin-left: .45rem; }}
  .fix-note {{ color: var(--muted); font-style: italic; }}

  #search {{ width: 100%; font-family: "Source Sans 3", sans-serif; font-size: .95rem; color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: .55rem .9rem; margin-bottom: .7rem; }}
  #search:focus-visible {{ outline: 2px solid var(--blue); outline-offset: 1px; }}
  #search::placeholder {{ color: var(--muted); }}

  .tagpill {{ border: 1px solid var(--line); color: var(--muted); border-radius: 4px; font-size: .65rem; padding: .1rem .4rem; letter-spacing: .05em; text-transform: uppercase; }}

  .srcpanel {{ margin-top: .5rem; }}
  .srcpanel summary {{ font-family: "Space Grotesk", monospace; font-size: .78rem; color: var(--muted); cursor: pointer; user-select: none; padding: .2rem 0; }}
  .srcpanel summary:hover {{ color: var(--blue); }}
  #src-note {{ color: var(--blue); }}
  .srchint {{ font-size: .75rem; color: var(--muted); margin: .3rem 0 .5rem; }}
  .srcchips {{ display: flex; gap: .45rem; flex-wrap: wrap; }}
  .srcchip {{ font-family: "Space Grotesk", monospace; font-size: .74rem; border: 1px solid var(--line); background: var(--card); color: var(--ink); border-radius: 999px; padding: .25rem .7rem; cursor: pointer; }}
  .srcchip[aria-pressed="false"] {{ color: var(--muted); background: transparent; text-decoration: line-through; opacity: .6; }}
  .srcchip:focus-visible {{ outline: 2px solid var(--blue); outline-offset: 2px; }}

  .wrap {{ max-width: 720px; margin: 0 auto; padding: 1.2rem; }}

  .filters {{ display: flex; gap: .5rem; flex-wrap: wrap; padding-bottom: .4rem; }}
  .chip {{ font-family: "Space Grotesk", monospace; font-size: .78rem; border: 1px solid var(--line); background: var(--card); color: var(--muted); border-radius: 999px; padding: .3rem .8rem; cursor: pointer; }}
  .chip[aria-pressed="true"] {{ background: var(--blue); border-color: var(--blue); color: var(--badge-fg); }}
  .chip:focus-visible {{ outline: 2px solid var(--blue); outline-offset: 2px; }}

  .day {{ font-family: "Archivo", sans-serif; font-weight: 800; font-size: .85rem; text-transform: uppercase; letter-spacing: .12em; color: var(--blue); margin: 1.6rem 0 .4rem; }}

  .item {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: .95rem 1.1rem; margin-top: .6rem; }}
  .item.official {{ border-left: 3px solid var(--blue); }}
  .meta {{ display: flex; align-items: center; gap: .7rem; font-family: "Space Grotesk", monospace; font-size: .74rem; color: var(--muted); margin-bottom: .3rem; }}
  .src {{ color: var(--blue); }}
  .badge {{ background: var(--blue); color: var(--badge-fg); border-radius: 4px; font-size: .65rem; padding: .1rem .4rem; letter-spacing: .05em; text-transform: uppercase; }}
  .headline {{ font-family: "Archivo", sans-serif; font-weight: 700; font-variation-settings: "wdth" 96; font-size: 1.06rem; line-height: 1.3; color: var(--ink); text-decoration: none; display: block; }}
  .headline:hover {{ color: var(--blue); text-decoration: underline; text-underline-offset: 3px; }}
  .excerpt {{ font-size: .88rem; color: var(--muted); margin-top: .35rem; }}

  footer {{ max-width: 720px; margin: 2rem auto 3rem; padding: 0 1.2rem; font-size: .8rem; color: var(--muted); }}
  footer a {{ color: var(--blue); }}
  .hidden {{ display: none; }}
  @media (prefers-reduced-motion: no-preference) {{
    .item {{ transition: border-color .15s; }}
    .item:hover {{ border-color: var(--blue); }}
    body, .item, .chip, header {{ transition: background-color .2s, color .2s; }}
  }}
</style>
</head>
<body>
<header>
  <div class="mast">
    <div class="wordmark">
      <div class="wm-rule"></div>
      <div class="wm-the">The</div>
      <div class="wm-wednesday">Wednesday</div>
      <div class="wm-times">Times</div>
      <div class="wm-rule"></div>
    </div>
    <div class="mast-right">
      <div class="tag">No ads · links to sources</div>
      <button id="theme-toggle" aria-label="Switch between light and dark mode">Dark</button>
    </div>
  </div>
  <div class="followbar"><div class="inner">
    <span>Official:</span>{follow}
  </div></div>
</header>

{fixtures_html}

<main class="wrap">
  <input id="search" type="search" placeholder="Search headlines…" aria-label="Search headlines">
  <div class="filters" role="group" aria-label="Filter the feed">
    <button class="chip" data-filter="all" aria-pressed="true">All</button>
    <button class="chip" data-filter="official">Official only</button>
    {tag_chips}
  </div>
  <details class="srcpanel">
    <summary>Sources <span id="src-note"></span></summary>
    <p class="srchint">Tap a source to hide it from your feed (tap again to bring it back). Remembered on this device.</p>
    <div class="srcchips">{chips}</div>
  </details>
  {items_html}
</main>

<footer>
  <p>Headlines link straight to the original publishers — read the full stories there.
  Updated {BUILT_AT_UK.strftime('%H:%M')} UK time, {BUILT_AT_UK.strftime('%d %b %Y')}.</p>
  <p>Independent and unofficial — not affiliated with Sheffield Wednesday FC or the EFL.
  Free and ad-free. If it's useful, <a href="https://ko-fi.com/allwednesday" target="_blank" rel="noopener">you can support it here</a>.</p>
</footer>

<script>
  // ---- theme ----
  const root = document.documentElement;
  const toggle = document.getElementById('theme-toggle');
  function applyTheme(t) {{
    root.setAttribute('data-theme', t);
    toggle.textContent = t === 'dark' ? 'Light' : 'Dark';
  }}
  let saved = null;
  try {{ saved = localStorage.getItem('theme'); }} catch (e) {{}}
  applyTheme(saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
  toggle.addEventListener('click', () => {{
    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try {{ localStorage.setItem('theme', next); }} catch (e) {{}}
  }});

  // ---- filters + search + source toggles (combined) ----
  const chips = document.querySelectorAll('.chip');
  const srcchips = document.querySelectorAll('.srcchip');
  const search = document.getElementById('search');
  const srcNote = document.getElementById('src-note');
  let activeFilter = 'all';

  let excluded = new Set();
  try {{ excluded = new Set(JSON.parse(localStorage.getItem('excludedSources') || '[]')); }} catch (e) {{}}

  function syncSrcChips() {{
    srcchips.forEach(c => c.setAttribute('aria-pressed', excluded.has(c.dataset.src) ? 'false' : 'true'));
    srcNote.textContent = excluded.size ? '(' + excluded.size + ' hidden)' : '';
  }}

  function applyView() {{
    const q = search.value.trim().toLowerCase();
    document.querySelectorAll('.item').forEach(item => {{
      let show = !excluded.has(item.dataset.source);
      if (show && activeFilter === 'official') show = item.dataset.official === '1';
      else if (show && activeFilter.startsWith('tag:')) show = item.dataset.tag === activeFilter.slice(4);
      if (show && q) show = item.textContent.toLowerCase().includes(q);
      item.classList.toggle('hidden', !show);
    }});
    document.querySelectorAll('.day').forEach(day => {{
      let el = day.nextElementSibling, any = false;
      while (el && !el.classList.contains('day')) {{
        if (el.classList.contains('item') && !el.classList.contains('hidden')) any = true;
        el = el.nextElementSibling;
      }}
      day.classList.toggle('hidden', !any);
    }});
  }}

  chips.forEach(chip => chip.addEventListener('click', () => {{
    chips.forEach(c => c.setAttribute('aria-pressed', c === chip));
    activeFilter = chip.dataset.filter;
    applyView();
  }}));

  srcchips.forEach(chip => chip.addEventListener('click', () => {{
    const s = chip.dataset.src;
    if (excluded.has(s)) excluded.delete(s); else excluded.add(s);
    try {{ localStorage.setItem('excludedSources', JSON.stringify([...excluded])); }} catch (e) {{}}
    syncSrcChips();
    applyView();
  }}));

  search.addEventListener('input', applyView);
  syncSrcChips();
  applyView();

  // ---- live relative time (recalculated in-browser, not baked in at
  // build time — keeps "Xm/Xh ago" accurate even if the page sits open
  // for hours between rebuilds) ----
  function relTimeFromISO(iso) {{
    const then = new Date(iso).getTime();
    const mins = Math.max(1, Math.round((Date.now() - then) / 60000));
    if (mins < 60) return mins + 'm ago';
    const hours = Math.round(mins / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.round(hours / 24) + 'd ago';
  }}
  function refreshTimes() {{
    document.querySelectorAll('.time[data-published]').forEach(el => {{
      el.textContent = relTimeFromISO(el.dataset.published);
    }});
  }}
  refreshTimes();
  setInterval(refreshTimes, 60000);
</script>
</body>
</html>"""

(HERE / "index.html").write_text(page)

# ---- our own RSS feed of the aggregated stream ----
def rfc822(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a, %d %b %Y %H:%M:%S +0000")

rss_items = "".join(
    f"""
  <item>
    <title>{html.escape(a['title'])}</title>
    <link>{html.escape(a['url'])}</link>
    <guid isPermaLink="true">{html.escape(a['url'])}</guid>
    <pubDate>{rfc822(a['published'])}</pubDate>
    <description>{html.escape((a.get('excerpt') or '') + ' — ' + a['source'])}</description>
  </item>"""
    for a in ARTICLES[:40]
)
rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>The Wednesday Times</title>
  <link>{SITE_URL}/</link>
  <description>Sheffield Wednesday headlines in one clean feed. Links go to the original publishers.</description>
  <language>en-gb</language>
  <lastBuildDate>{BUILT_AT.strftime("%a, %d %b %Y %H:%M:%S +0000")}</lastBuildDate>{rss_items}
</channel>
</rss>"""
(HERE / "feed.xml").write_text(rss)

# ---- sitemap.xml (for Google Search Console) ----
sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{BUILT_AT.strftime('%Y-%m-%d')}</lastmod>
    <changefreq>hourly</changefreq>
  </url>
</urlset>"""
(HERE / "sitemap.xml").write_text(sitemap)

n_official = sum(1 for a in ARTICLES if a.get("official"))
print(f"Built index.html + feed.xml + sitemap.xml: {len(ARTICLES)} articles ({n_official} official), tags: {', '.join(used_tags) or 'none'}")
