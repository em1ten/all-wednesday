#!/usr/bin/env python3
"""
The Wednesday Times — feed fetcher.

Pulls Sheffield Wednesday headlines from public RSS feeds, filters out the
junk, dedupes, and writes articles.json for the site builder. Also writes
status.json, a health report for this run (which feeds worked, what got
filtered and why) that the builder turns into /status.html.

We only store headline + short excerpt + link out to the source — we never
republish article content. That keeps it clean legally and sends readers
(and the ad revenue) to the publishers.

Run:  python3 fetch_news.py
"""

from __future__ import annotations

import calendar
import html as htmllib
import ipaddress
import json
import os
import re
import socket
import time
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import feedparser
import requests

HERE = Path(__file__).parent

FEEDS = [
    {
        "source": "BBC Sport",
        "url": "https://feeds.bbci.co.uk/sport/football/teams/sheffield-wednesday/rss.xml",
        "official": False,
    },
    {
        # Google News aggregates The Star, Yorkshire Live, Sky Sports,
        # The Athletic etc. — widest net with one feed.
        "source": "Google News",
        "url": 'https://news.google.com/rss/search?q="Sheffield+Wednesday"&hl=en-GB&gl=GB&ceid=GB:en',
        "official": False,
    },
    {
        # The Star's own Wednesday feed. Google News already carries most
        # of these, but the direct feed gives a real summary, a picture and
        # a link straight to the article (not via Google's redirect).
        # Duplicates of the Google copies are merged in dedupe().
        "source": "The Star",
        "url": "https://www.thestar.co.uk/sport/football/sheffield-wednesday/rss",
        "official": False,
    },
    # ---- OFFICIAL sources (club / league) ----
    {
        # Club website news, via a Google News query restricted to swfc.co.uk
        "source": "SWFC Official",
        "url": "https://news.google.com/rss/search?q=site:swfc.co.uk&hl=en-GB&gl=GB&ceid=GB:en",
        "official": True,
    },
    {
        # Official YouTube channel — YouTube still provides free RSS.
        "source": "SWFC YouTube",
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCXRpYvFmY12TMKet-E0w_Cw",
        "official": True,
    },
    {
        # League news mentioning the club
        "source": "EFL Official",
        "url": "https://news.google.com/rss/search?q=site:efl.com+%22Sheffield+Wednesday%22&hl=en-GB&gl=GB&ceid=GB:en",
        "official": True,
    },
    # Add direct feeds here as you find them. Same shape — set
    # "official": False for media/fan sources.
]

MAX_AGE_DAYS = 7
EXCERPT_CHARS = 400
MIN_EXCERPT_CHARS = 30        # shorter "summaries" are teaser labels ("Loan watch")
FUTURE_TOLERANCE_SECS = 600   # timestamps further ahead than this are clamped to now
OUT = HERE / "articles.json"
STATUS = HERE / "status.json"

# ---- network limits (a slow or huge response can never hang a build) ----
FEED_TIMEOUT = 15             # seconds to connect / between bytes
FEED_DEADLINE = 30            # seconds for the whole download
FEED_MAX_BYTES = 5_000_000    # a Wednesday RSS feed is ~100 KB

# ---- article description cache ----
# Publishers' own og:description tags (the one-liners they write for
# link previews - what Twitter/WhatsApp show). Cached by URL so each
# article is only ever fetched once, not on every 15-minute run.
# Conservatively tuned so this can never hang or noticeably slow a build.
# Google News links are skipped: they point at Google's redirect page,
# not the article, so there's no publisher description to read.
DESC_CACHE = HERE / "descriptions.json"
DESC_MAX_NEW_PER_RUN = 12   # hard cap on new fetches per run
DESC_TIMEOUT = 6            # seconds per request
DESC_MAX_CHARS = 200
DESC_SCAN_BYTES = 300_000   # og tags live in <head>; never read further
DESC_RETRY_AFTER_SECS = 86_400  # retry a failed fetch after a day

# Normalise the scruffy names Google News reports so the source list
# stays tidy (one chip per outlet, proper names not domains).
SOURCE_ALIASES = {
    "thestar.co.uk": "The Star",
    "Sheffield Star": "The Star",
    "BBC Sport": "BBC",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
    "portsmouth.co.uk": "The News (Portsmouth)",
    "yorkshirepost.co.uk": "Yorkshire Post",
    "skysports.com": "Sky Sports",
    "theguardian.com": "The Guardian",
    "Sheffield Wednesday FC": "SWFC Official",
    "The English Football League": "EFL Official",
}

# Any article whose (normalised) source is one of these gets the
# Official badge, however it arrived.
OFFICIAL_SOURCE_NAMES = {"SWFC Official", "SWFC YouTube", "EFL Official"}

FETCH_HEADERS = {
    # A plain/default user-agent gets rate-limited or blocked by Google News
    # more often than a normal browser identity does. This isn't foolproof,
    # but it noticeably reduces silent empty-feed failures.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# =====================================================================
# Small helpers
# =====================================================================

def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = htmllib.unescape(text)          # &nbsp; &amp; &#39; etc -> real chars
    text = text.replace("\xa0", " ")       # non-breaking spaces -> spaces
    return re.sub(r"\s+", " ", text).strip()


def safe_url(url, *, https_only: bool = False) -> str | None:
    """Only ordinary web links get through: http(s), a real host, no
    embedded credentials or whitespace. Anything else (javascript:,
    data:, relative junk) is dropped before it can reach the page."""
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
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme.lower() not in allowed or not host:
        return None
    if parts.username or parts.password:
        return None
    # %-encode anything that isn't valid in a URL (quotes, angle brackets)
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")


def is_google_link(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith("news.google.com")


def real_source(entry, fallback: str) -> str:
    """Google News entries carry the actual publisher in entry.source."""
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return clean_html(src.title)
    return fallback


_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|\?)", re.IGNORECASE)


def extract_image(entry) -> str | None:
    """Best-effort thumbnail URL from whichever RSS/Atom convention the
    feed happens to use. Purely cosmetic — if nothing matches, the card
    just renders without an image. https only, so the page never pulls
    in insecure content."""
    candidates = []
    # Media RSS extension (common on BBC and many publisher feeds) -
    # feedparser can expose this as either a list or a single dict
    # depending on the feed, so handle both shapes defensively.
    thumb = getattr(entry, "media_thumbnail", None)
    if isinstance(thumb, dict):
        thumb = [thumb]
    for t in thumb or []:
        if isinstance(t, dict):
            candidates.append(t.get("url"))
    for c in getattr(entry, "media_content", None) or []:
        if not isinstance(c, dict):
            continue
        url = c.get("url") or ""
        kind = f"{c.get('medium', '')} {c.get('type', '')}"
        if "image" in kind or _IMAGE_EXT.search(url):
            candidates.append(url)
    # Podcast/enclosure-style image link
    for link in getattr(entry, "links", None) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            candidates.append(link.get("href"))
    # Last resort: an <img> tag embedded directly in the HTML body
    html_sources = [getattr(entry, "summary", "") or ""]
    for c in getattr(entry, "content", None) or []:
        html_sources.append(c.get("value", ""))
    for html_src in html_sources:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_src)
        if m:
            candidates.append(htmllib.unescape(m.group(1)))
    for url in candidates:
        good = safe_url(url, https_only=True)
        if good:
            return good
    return None


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file so a crash mid-write can't leave a half
    file behind for the next step (or the next run) to choke on."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# =====================================================================
# Filters
# =====================================================================

# Google News' relevance matching sometimes lets Sheffield United ("the
# Blades") stories through since they share a city and league. Drop a
# story if it's clearly about United and doesn't also mention Wednesday
# (genuine derby/crossover stories mentioning both are kept).
UNITED_MARKERS = ("sheffield united", "blades", "sufc")
WEDNESDAY_MARKERS = ("sheffield wednesday", "swfc", "owls", "hillsborough")
STRONG_WEDNESDAY_MARKERS = ("sheffield wednesday", "swfc")

# The Star files United stories under labels like "SUFC News:". Those are
# United stories even when Wednesday gets a passing mention ("...as Owls
# comparison rejected"), so they need Wednesday named in the headline.
_UNITED_LABEL = re.compile(r"^\s*(?:sufc|sheffield united|blades)\b[^:]{0,12}:")

# "ex-Owls striker", "former Sheffield Wednesday defender" describe a
# player's past, not a Wednesday story (the Wrexham ex-player problem),
# so on their own they don't count as a Wednesday mention.
_EX_PLAYER = re.compile(
    r"\b(?:ex|former)[\s-]+(?:sheffield wednesday|swfc|owls?)(?:'s?)?(?=\W|$)"
)

# A bare "Wednesday" is the club in "Wednesday 4-1 Wigan" or "Wednesday's
# comeback", but the weekday in "on Wednesday" or "Wednesday night".
_BARE_WEDNESDAY = re.compile(r"\bwednesday\b")
_WEEKDAY_BEFORE = re.compile(
    r"\b(?:on|this|next|last|until|till|by|from|since|every|each|before|after|"
    r"midweek|monday|tuesday|thursday|friday|saturday|sunday)[\s,]+$"
)
_WEEKDAY_AFTER = re.compile(
    r"^(?:\s+(?:night|evening|morning|afternoon|lunchtime)\b|"
    r",?\s+\d{1,2}(?:st|nd|rd|th)?\b|,?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b)"
)


def _club_wednesday(text: str) -> bool:
    for m in _BARE_WEDNESDAY.finditer(text):
        before = text[max(0, m.start() - 16):m.start()]
        after = text[m.end():m.end() + 24]
        if before.endswith("sheffield "):
            return True
        if _WEEKDAY_BEFORE.search(before) or _WEEKDAY_AFTER.match(after):
            continue
        return True
    return False


def mentions_wednesday(text: str) -> bool:
    text = _EX_PLAYER.sub(" ", text.lower())
    if any(m in text for m in WEDNESDAY_MARKERS):
        return True
    return _club_wednesday(text)


def is_wrong_club(title: str, excerpt: str) -> bool:
    text = f"{title} {excerpt}".lower()
    if not any(m in text for m in UNITED_MARKERS):
        return False
    lowered_title = title.lower()
    if _UNITED_LABEL.match(lowered_title):
        return not any(m in lowered_title for m in STRONG_WEDNESDAY_MARKERS)
    return not mentions_wednesday(text)


def is_off_topic(title: str, excerpt: str) -> bool:
    """For general/loosely-matched feeds (BBC team feed, Google News): if
    the story doesn't mention Wednesday in any recognised form, it's not
    really about Wednesday, whichever other club or player it follows.
    (e.g. a former player's news at their new club can slip through team-
    tagged feeds). Official feeds skip this check — they're already
    scoped by URL, and short titles like "Highlights" or "Pre-season in
    Hungary!" are legitimately on-topic without repeating the club name.
    """
    return not mentions_wednesday(f"{title} {excerpt}")


# Known domains hijacked/repurposed to host illegal sports-streaming
# spam (e.g. a compromised .ac.jp university subdomain, an unrelated
# expired-and-repurposed .co.uk). This list is a backstop, not the main
# defence - is_stream_spam() below catches the pattern generically so
# new spam domains don't need adding here one at a time.
SPAM_DOMAINS = {"rikkyo.ac.jp", "infrastructure-now.co.uk"}

# Decorative Unicode used to dodge basic keyword filters: Fullwidth
# Forms (e.g. "Ｌ Ｉ Ｖ Ｅ") and Mathematical Alphanumeric Symbols
# (fake-bold "𝐓𝐕"). Legitimate football journalism essentially never
# uses these - a very reliable spam signal on its own.
_DECORATIVE_UNICODE = re.compile(r"[＀-￯\U0001D400-\U0001D7FF]")

# Piracy wording. Real "how to watch" guides say "live stream, TV
# channel"; pirates add "free", "reddit", "HD", "(LIVE^NOW)" and so on.
_STREAM_SPAM_WORDS = re.compile(
    r"\b(?:live\s*streams?|livestreams?|streams?)\b.{0,60}\b(?:free|reddit|hd|crackstreams?)\b"
    r"|\b(?:free|reddit)\b.{0,30}\blive\s*streams?\b"
    r"|\(\s*live\s*\^?\s*now\s*\)|\blive\s*\^\s*now\b|\[\s*live",
    re.IGNORECASE,
)
# A matchup ("X vs Sheffield Wednesday") published under a university
# domain is the hijacked-subdomain pattern; real university news isn't
# written as a fixture.
_ACADEMIC_HOST = re.compile(r"\.(?:ac|edu)\.[a-z]{2}$|\.edu$")
_MATCHUP = re.compile(r"\bvs?\.?\s+sheffield wednesday\b|\bsheffield wednesday\s+vs?\.?\s")


def is_stream_spam(title: str, excerpt: str, source: str) -> bool:
    """Illegal live-stream piracy spam: decorative unicode to dodge
    filters, the same matchup phrase repeated several times (bot-
    generated filler text), piracy wording, or a hijacked domain."""
    text = f"{title} {excerpt}"
    lower = text.lower()
    src = source.lower().strip()

    if _DECORATIVE_UNICODE.search(text):
        return True
    if any(domain in src for domain in SPAM_DOMAINS):
        return True
    # Real journalism doesn't repeat "Sheffield Wednesday" (or similar)
    # three-plus times in one headline+excerpt - bot filler text does.
    if lower.count("sheffield wednesday") >= 3:
        return True
    if _STREAM_SPAM_WORDS.search(text):
        return True
    if _ACADEMIC_HOST.search(src) and _MATCHUP.search(lower):
        return True
    return False


# Bookmaker promotions ("Get £30 in Free Bets...") are adverts, which an
# ad-free site shouldn't carry. Ordinary previews that mention odds stay.
# Set to False to let them back in. Every story this removes is counted
# on /status.html, so the filtering is never silent.
BLOCK_BETTING_PROMOS = True
BETTING_SOURCES = {
    "footy accumulators", "william hill news", "paddy power news", "betfair",
    "sky bet", "oddschecker", "olbg", "bettingexpert", "betway", "bet365",
    "ladbrokes", "coral", "boylesports", "betvictor",
}
_BETTING_WORDS = re.compile(
    r"\bfree bets?\b|\bbet ?builder\b|\bbetting tips?\b|\bacca tips?\b|\bsign[- ]up offer\b",
    re.IGNORECASE,
)


def is_betting_promo(title: str, source: str) -> bool:
    return source.lower().strip() in BETTING_SOURCES or bool(_BETTING_WORDS.search(title))


# ---- headline clean-up ----
# Some feeds scrape the publisher's own page furniture into the
# headline, e.g. "Real headline Club News | 58 minutes ago".
_PAGE_FURNITURE = re.compile(
    r"\s*(Club News|News|Video|Match Report|Interview)\s*\|\s*\d+\s+"
    r"(second|minute|hour|day|week|month)s?\s+ago.*$",
    re.IGNORECASE,
)
_DOMAIN_TAIL = re.compile(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\s+uk)?", re.IGNORECASE)
GENERIC_TITLES = {
    "sheffield wednesday", "sheffield wednesday fc", "swfc", "owls", "the owls",
    "news", "latest news", "video", "home", "untitled",
}
BOILERPLATE_EXCERPTS = {"sheffield wednesday official video."}


def clean_title(title: str, publisher: str | None = None, via_google: bool = False) -> str:
    t = _PAGE_FURNITURE.sub("", title).strip()
    if via_google:
        # Google News appends " - Publisher". Remove exactly that when we
        # know the publisher (so a real " - " in the headline survives).
        if publisher and t.endswith(f" - {publisher}"):
            t = t[: -len(publisher) - 3]
        elif publisher and t.strip(" -–—|") == publisher:
            t = ""
        elif not publisher:
            t = re.sub(r"\s+-\s+[^-]+$", "", t)
    # Trailing site names that aren't the headline: " | Goal.com UK",
    # " - infrastructure-now.co.uk"
    for sep in (" | ", " - ", " – ", " — "):
        if sep in t:
            head, tail = t.rsplit(sep, 1)
            tail_l = tail.strip().lower()
            pub_l = (publisher or "").lower()
            if len(tail_l) <= 40 and (
                _DOMAIN_TAIL.fullmatch(tail_l)
                or (pub_l and (tail_l == pub_l or tail_l.startswith(pub_l + " ")))
            ):
                t = head
            break
    return t.strip(" -–—|:·").strip()


def is_junk_title(title: str, source: str) -> bool:
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return len(words) < 8 or words in GENERIC_TITLES or words == source.lower()


def clean_excerpt(excerpt: str, title: str, source: str) -> str:
    e = excerpt.strip()
    if not e or len(e) < MIN_EXCERPT_CHARS:
        return ""
    lower = e.lower()
    if lower in BOILERPLATE_EXCERPTS or lower == source.lower():
        return ""
    # Many feeds just repeat the headline as the summary
    if title and lower.startswith(title.lower()[:50]):
        return ""
    return e


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def filter_reason(title: str, excerpt: str, source: str, official: bool) -> str | None:
    """Why a story is being left out, or None to keep it. The reason
    names are what /status.html counts."""
    if not title:
        return "junk_title"
    if is_stream_spam(title, excerpt, source):
        return "stream_spam"
    if BLOCK_BETTING_PROMOS and is_betting_promo(title, source):
        return "betting"
    if is_wrong_club(title, excerpt):
        return "sheffield_united"
    if not official and is_off_topic(title, excerpt):
        return "off_topic"
    return None


# =====================================================================
# Fetching
# =====================================================================

class FetchError(Exception):
    pass


def download(url: str, *, timeout: float, deadline: float, max_bytes: int,
             truncate: bool = False, allow_redirects: bool = True):
    """GET with hard limits. Returns (response, body bytes). Oversized
    bodies raise FetchError, or are cut short when truncate=True (fine
    for scanning an HTML <head>, never for XML)."""
    started = time.monotonic()
    with requests.get(url, headers=FETCH_HEADERS, timeout=timeout, stream=True,
                      allow_redirects=allow_redirects) as r:
        chunks, size = [], 0
        while True:
            # read1 returns whatever has arrived rather than waiting for a
            # full 64 KB, so a server dripping a few bytes at a time still
            # hits the deadline check below
            chunk = r.raw.read1(64 * 1024, decode_content=True)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size >= max_bytes:
                if truncate:
                    break
                raise FetchError(f"response bigger than {max_bytes // 1000} KB")
            if time.monotonic() - started > deadline:
                raise FetchError(f"download took longer than {deadline}s")
        return r, b"".join(chunks)


def entry_timestamp(entry) -> float | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            # feedparser's *_parsed values are UTC. calendar.timegm reads
            # them as UTC; time.mktime would read them as local time and
            # shift every story by an hour on a UK machine in summer.
            return float(calendar.timegm(parsed))
    return None


def build_article(feed: dict, entry, now: float) -> tuple[dict | None, str | None]:
    """One feed entry -> (article, None), (None, filter reason), or
    (None, None) for entries that are simply too old or undated."""
    ts = entry_timestamp(entry)
    if ts is None or (now - ts) > MAX_AGE_DAYS * 86400:
        return None, None
    if ts > now + FUTURE_TOLERANCE_SECS:
        ts = now  # bad timezone in the feed; don't let it squat at the top

    link = safe_url(entry.get("link"))
    if not link:
        return None, "bad_link"

    via_google = feed.get("via_google", is_google_link(feed["url"]))
    publisher = real_source(entry, feed["source"])
    source = feed["source"] if feed["official"] else publisher
    source = SOURCE_ALIASES.get(source, source)
    official = bool(feed["official"] or source in OFFICIAL_SOURCE_NAMES)

    title = clean_title(clean_html(entry.get("title", "")),
                        publisher if via_google else None, via_google)
    # Google News "summaries" are just the headline again (or a cluster of
    # other outlets' headlines mashed together), never a real summary.
    raw_excerpt = "" if via_google else clean_html(entry.get("summary", ""))
    excerpt = clean_excerpt(shorten(raw_excerpt, EXCERPT_CHARS), title, source)

    if is_junk_title(title, source):
        # e.g. BBC video items titled just "Sheffield Wednesday": the
        # summary is the real headline. Otherwise there's nothing to show.
        if excerpt:
            title, excerpt = shorten(excerpt, 160).rstrip("."), ""
        else:
            return None, "junk_title"

    reason = filter_reason(title, excerpt, source, official)
    if reason:
        return None, reason
    return {
        "title": title,
        "url": link,
        "source": source,
        "published": datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(),
        "excerpt": excerpt,
        "official": official,
        "image": extract_image(entry),
    }, None


def fetch_all(feed_stats: list, filtered: Counter, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    articles = []
    for feed in FEEDS:
        stat = {"source": feed["source"], "host": urlsplit(feed["url"]).hostname,
                "ok": False, "http_status": None, "entries": 0, "kept": 0, "error": None}
        started = time.monotonic()
        try:
            r, body = download(feed["url"], timeout=FEED_TIMEOUT, deadline=FEED_DEADLINE,
                               max_bytes=FEED_MAX_BYTES)
            stat["http_status"] = r.status_code
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code}")
            parsed = feedparser.parse(body, response_headers={
                "content-type": r.headers.get("Content-Type", "application/xml")})
            entries = list(parsed.entries)
            stat["entries"] = len(entries)
            if not entries:
                problem = parsed.get("bozo_exception")
                raise FetchError(f"no entries ({problem})" if problem else "feed returned no entries")
            for e in entries:
                art, reason = build_article(feed, e, now)
                if reason:
                    filtered[reason] += 1
                elif art:
                    articles.append(art)
                    stat["kept"] += 1
            stat["ok"] = True
        except Exception as e:  # one broken feed must never stop the others
            stat["error"] = (str(e) or type(e).__name__)[:200]
        stat["ms"] = int((time.monotonic() - started) * 1000)
        feed_stats.append(stat)
        if stat["ok"]:
            print(f"  [ok] {feed['source']}: {stat['entries']} entries, {stat['kept']} kept")
        else:
            warn(f"{feed['source']} feed failed: {stat['error']}")
    return articles


# =====================================================================
# Merge, dedupe, carry-over
# =====================================================================

def _story_key(title: str) -> str:
    t = title.lower()
    t = re.sub(r"^\s*(?:swfc|sufc)(?:\s+news)?\s*:\s*", "", t)  # The Star's section labels
    t = t.replace("swfc", "sheffield wednesday")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _preference(a: dict) -> tuple:
    """Which copy of the same story to show. Not a ranking of stories -
    just picks between duplicates: official first, then a direct link
    (not via Google), then one with a summary, then one with a picture."""
    return (bool(a.get("official")), not is_google_link(a["url"]),
            bool(a.get("excerpt") or a.get("description")), bool(a.get("image")))


def dedupe(articles: list[dict]) -> list[dict]:
    """Same story from multiple feeds/outlets: keep one copy."""
    # 1) the exact same link (e.g. this run and last run): first one wins -
    #    fresh items come first - but keep any picture/summary the other had
    by_url: dict[str, dict] = {}
    for a in articles:
        kept = by_url.get(a["url"])
        if kept is None:
            by_url[a["url"]] = a
            continue
        for field in ("image", "excerpt", "description"):
            if not kept.get(field) and a.get(field):
                kept[field] = a[field]

    # 2) near-identical headlines (fuzzy match), oldest first
    items = sorted(by_url.values(), key=lambda a: a["published"])
    kept_items: list[dict] = []
    keys: list[str] = []
    for a in items:
        key = _story_key(a["title"])
        match = next((i for i, k in enumerate(keys)
                      if SequenceMatcher(None, key, k).ratio() > 0.75), None)
        if match is None:
            kept_items.append(a)
            keys.append(key)
        elif _preference(a) > _preference(kept_items[match]):
            kept_items[match] = a
            keys[match] = key
    kept_items.sort(key=lambda a: a["published"], reverse=True)
    return kept_items


def normalise_previous(a, now: float) -> dict | None:
    """Re-check a carried-over article against today's rules, so a filter
    added later also cleans up stories that were already on the site."""
    if not isinstance(a, dict):
        return None
    try:
        title, source = str(a["title"]), str(a["source"])
        ts = datetime.fromisoformat(a["published"]).timestamp()
    except Exception:
        return None
    if (now - ts) > MAX_AGE_DAYS * 86400:
        return None
    url = safe_url(a.get("url"))
    if not url:
        return None
    official = bool(a.get("official"))
    excerpt = "" if is_google_link(url) else str(a.get("excerpt") or "")
    title = clean_title(title, source)
    if is_junk_title(title, source) or filter_reason(title, excerpt, source, official):
        return None
    out = dict(a)
    out.update(title=title, url=url, excerpt=excerpt, official=official,
               image=safe_url(a.get("image"), https_only=True))
    out.pop("description", None)  # re-applied from the cache
    return out


def load_previous(now: float | None = None) -> list[dict]:
    """Last run's articles (restored from the live site by
    restore_state.py before this runs), filtered to the same age window
    as fresh fetches - the fallback when a feed is temporarily blocked.
    Missing/corrupt file or a bad entry just drops that entry, never fatal."""
    now = time.time() if now is None else now
    try:
        old = json.loads(OUT.read_text())
    except Exception:
        return []
    if not isinstance(old, list):
        return []
    kept = []
    for a in old:
        norm = normalise_previous(a, now)
        if norm:
            kept.append(norm)
    return kept


# =====================================================================
# Publisher descriptions (og:description), cached
# =====================================================================

def load_desc_cache() -> dict:
    """URL -> {"d": description, "t": unix time it was checked}.
    Old caches stored plain strings: a non-empty one is kept, an empty
    one (a past failure) is retried. Missing/corrupt file: start fresh."""
    try:
        raw = json.loads(DESC_CACHE.read_text())
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    cache = {}
    for url, v in raw.items():
        if isinstance(v, str):
            if v:
                cache[url] = {"d": v, "t": 0}
        elif isinstance(v, dict) and isinstance(v.get("d"), str):
            try:
                cache[url] = {"d": v["d"], "t": float(v.get("t") or 0)}
            except (TypeError, ValueError):
                continue
    return cache


def prune_desc_cache(cache: dict, articles: list[dict]) -> dict:
    """Drop cached descriptions for articles no longer in the feed, so the
    file can't grow forever. Articles age out after MAX_AGE_DAYS anyway,
    so anything not in the current set is genuinely gone."""
    live_urls = {a["url"] for a in articles}
    return {url: v for url, v in cache.items() if url in live_urls}


def is_public_http_url(url: str) -> bool:
    """Guard for fetching URLs that came out of a feed: only http(s) to
    hosts that resolve to public internet addresses, so a hostile feed
    can't point the build at localhost, the cloud metadata service or
    anything else on a private network."""
    if not safe_url(url):
        return False
    parts = urlsplit(url)
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def description_from_html(html_text: str) -> str | None:
    found = {}
    for tag in _META_TAG.findall(html_text):
        attrs = {m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "")
                 for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in ("og:description", "twitter:description", "description") and key not in found:
            found[key] = attrs.get("content", "")
    for key in ("og:description", "twitter:description", "description"):
        desc = clean_html(found.get(key, ""))
        if len(desc) > 20:  # ignore uselessly short/placeholder text
            return shorten(desc, DESC_MAX_CHARS)
    return None


def fetch_description(url: str) -> str | None:
    """The publisher's own og:description - the summary they write for
    link previews. Returns None on any failure; the card just shows the
    headline alone. Redirects are followed by hand so every hop passes
    the public-address check."""
    try:
        for _ in range(4):
            if not is_public_http_url(url):
                return None
            r, body = download(url, timeout=DESC_TIMEOUT, deadline=DESC_TIMEOUT * 2,
                               max_bytes=DESC_SCAN_BYTES, truncate=True, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status_code != 200:
                return None
            return description_from_html(body.decode("utf-8", errors="replace"))
        return None
    except Exception:
        return None


def add_descriptions(articles: list[dict], now: float | None = None) -> dict:
    """Fill in descriptions from cache, fetching a capped number of new
    ones per run. Mutates articles in place. Returns counts for the
    status report."""
    now = time.time() if now is None else now
    cache = prune_desc_cache(load_desc_cache(), articles)

    fetched = 0
    for a in articles:
        url = a["url"]
        entry = cache.get(url)
        if entry and (entry["d"] or now - entry["t"] < DESC_RETRY_AFTER_SECS):
            if entry["d"] and not a.get("excerpt"):
                a["description"] = entry["d"]
            continue
        if a.get("excerpt") or is_google_link(url):
            continue          # already has a summary / nothing to fetch
        if fetched >= DESC_MAX_NEW_PER_RUN:
            continue          # cap reached; it'll be picked up next run
        desc = fetch_description(url)
        if desc and desc.lower().startswith(a["title"].lower()[:50]):
            desc = None       # just the headline again
        cache[url] = {"d": desc or "", "t": now}   # cache failures too (retried after a day)
        if desc:
            a["description"] = desc
        fetched += 1

    try:
        atomic_write(DESC_CACHE, json.dumps(cache, indent=1))
    except Exception as e:
        warn(f"could not write description cache: {e}")
    print(f"  descriptions: {fetched} newly fetched, {len(cache)} cached total")
    return {"fetched": fetched, "cached": len(cache)}


# =====================================================================
# Reporting
# =====================================================================

def warn(message: str) -> None:
    """A warning that shows up as a yellow annotation on the Actions run
    (and as plain text when run locally)."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=The Wednesday Times::{message}")
    else:
        print(f"  [WARN] {message}")


def write_status(feed_stats: list, filtered: Counter, counts: dict) -> None:
    status = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feeds": feed_stats,
        "filtered": dict(sorted(filtered.items())),
        "articles": counts,
    }
    try:
        atomic_write(STATUS, json.dumps(status, indent=2))
    except Exception as e:
        warn(f"could not write status.json: {e}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["### Feeds", "", "| Feed | Result | Entries | Kept |", "|---|---|---|---|"]
        for f in feed_stats:
            result = "ok" if f["ok"] else f"**failed**: {f['error']}"
            lines.append(f"| {f['source']} | {result} | {f['entries']} | {f['kept']} |")
        lines += ["", f"Filtered out: {dict(filtered) or 'nothing'}",
                  f"Articles: {counts}", ""]
        try:
            with open(summary, "a") as fh:
                fh.write("\n".join(lines) + "\n")
        except Exception:
            pass


def main() -> int:
    now = time.time()
    feed_stats: list = []
    filtered: Counter = Counter()
    fresh = fetch_all(feed_stats, filtered, now)
    previous = load_previous(now)
    # Merge rather than replace: if a feed returned nothing this run
    # (temporary block/rate-limit), its recent stories from last run's
    # data are still here to fall back on rather than the site briefly
    # losing content. dedupe() collapses duplicates and MAX_AGE_DAYS
    # keeps genuinely old stories from lingering.
    articles = dedupe(fresh + previous)
    desc_counts = add_descriptions(articles, now)
    counts = {"fresh": len(fresh), "carried_over": len(previous),
              "total": len(articles), "descriptions": desc_counts}
    write_status(feed_stats, filtered, counts)

    if not fresh:
        warn("no fresh stories from any feed this run - showing last run's stories")
    if not articles:
        # Never replace a good articles.json with an empty one. Failing
        # here stops the workflow before it can deploy an empty site.
        print("::error::No articles at all (every feed failed and nothing to carry over) "
              "- leaving articles.json untouched and stopping.")
        return 1
    atomic_write(OUT, json.dumps(articles, indent=2))
    print(f"Fetched {len(fresh)} fresh, {len(previous)} carried over, {len(articles)} total -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
