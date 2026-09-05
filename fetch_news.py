#!/usr/bin/env python3
"""
The Wednesday Times — feed fetcher.

Pulls Sheffield Wednesday headlines from public RSS feeds, dedupes them,
and writes articles.json for the site builder.

We only store headline + short excerpt + link out to the source — we never
republish article content. That keeps it clean legally and sends readers
(and the ad revenue) to the publishers.

Run:  python3 fetch_news.py
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import feedparser

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
    # ---- OFFICIAL sources (club / league) ----
    {
        # Club website news, via a Google News query restricted to swfc.co.uk
        "source": "SWFC Official",
        "url": "https://news.google.com/rss/search?q=site:swfc.co.uk&hl=en-GB&gl=GB&ceid=GB:en",
        "official": True,
    },
    {
        # Official YouTube channel — YouTube still provides free RSS.
        # Find the channel ID: open the channel page, View Source, search
        # for "channelId" (starts with UC...), then replace below.
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
    # Add direct feeds here as you find them (The Star, Yorkshire Live,
    # fan sites). Same shape — set "official": False for media/fan sources.
]

MAX_AGE_DAYS = 7
EXCERPT_CHARS = 400
OUT = Path(__file__).parent / "articles.json"

# ---- article description cache ----
# Publishers' own og:description tags (the one-liners they write for
# link previews - what Twitter/WhatsApp show). Cached by URL so each
# article is only ever fetched once, not on every 15-minute run.
# Conservatively tuned so this can never hang or noticeably slow a build.
DESC_CACHE = Path(__file__).parent / "descriptions.json"
DESC_MAX_NEW_PER_RUN = 12   # hard cap on new fetches per run
DESC_TIMEOUT = 6            # seconds per request
DESC_MAX_CHARS = 200

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


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = htmllib.unescape(text)          # &nbsp; &amp; &#39; etc -> real chars
    text = text.replace("\xa0", " ")       # non-breaking spaces -> spaces
    return re.sub(r"\s+", " ", text).strip()


def real_source(entry, fallback: str) -> str:
    """Google News entries carry the actual publisher in entry.source."""
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return clean_html(src.title)
    return fallback


def extract_image(entry) -> str | None:
    """Best-effort thumbnail URL from whichever RSS/Atom convention the
    feed happens to use. Purely cosmetic — if nothing matches, the card
    just renders without an image, same as it does today."""
    # Media RSS extension (common on BBC and many publisher feeds) -
    # feedparser can expose this as either a list or a single dict
    # depending on the feed, so handle both shapes defensively.
    thumb = getattr(entry, "media_thumbnail", None)
    if thumb:
        if isinstance(thumb, list) and thumb and thumb[0].get("url"):
            return thumb[0]["url"]
        if isinstance(thumb, dict) and thumb.get("url"):
            return thumb["url"]
    content = getattr(entry, "media_content", None)
    if content and isinstance(content, list):
        for c in content:
            if c.get("url") and ("image" in c.get("medium", "") or c.get("type", "").startswith("image")):
                return c["url"]
    # Podcast/enclosure-style image link
    for link in getattr(entry, "links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
            return link.get("href")
    # Last resort: an <img> tag embedded directly in the HTML body -
    # check both summary and the fuller content field, and accept
    # either quote style around the src attribute.
    html_sources = [getattr(entry, "summary", "") or ""]
    for c in getattr(entry, "content", []) or []:
        html_sources.append(c.get("value", ""))
    for html_src in html_sources:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_src)
        if m:
            return m.group(1)
    return None


FETCH_HEADERS = {
    # A plain/default user-agent gets rate-limited or blocked by Google News
    # more often than a normal browser identity does. This isn't foolproof,
    # but it noticeably reduces silent empty-feed failures.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Google News' relevance matching sometimes lets Sheffield United ("the
# Blades") stories through since they share a city and league. Drop a
# story if it's clearly about United and doesn't also mention Wednesday
# (genuine derby/crossover stories mentioning both are kept).
UNITED_MARKERS = ("sheffield united", "blades", "sufc")
WEDNESDAY_MARKERS = ("sheffield wednesday", "swfc", "owls", "hillsborough")


def is_wrong_club(title: str, excerpt: str) -> bool:
    text = f"{title} {excerpt}".lower()
    mentions_united = any(m in text for m in UNITED_MARKERS)
    mentions_wednesday = any(m in text for m in WEDNESDAY_MARKERS)
    return mentions_united and not mentions_wednesday


def is_off_topic(title: str, excerpt: str) -> bool:
    """For general/loosely-matched feeds (BBC team feed, Google News): if
    the story doesn't mention Wednesday in any recognised form, it's not
    really about Wednesday, whichever other club or player it follows.
    (e.g. a former player's news at their new club can slip through team-
    tagged feeds). Official feeds skip this check — they're already
    scoped by URL, and short titles like "Highlights" or "Pre-season in
    Hungary!" are legitimately on-topic without repeating the club name.
    """
    text = f"{title} {excerpt}".lower()
    return not any(m in text for m in WEDNESDAY_MARKERS)


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
_DECORATIVE_UNICODE = re.compile(r"[\uFF00-\uFFEF\U0001D400-\U0001D7FF]")


def is_stream_spam(title: str, excerpt: str, source: str) -> bool:
    """Illegal live-stream piracy spam: decorative unicode to dodge
    filters, the same matchup phrase repeated several times (bot-
    generated filler text), or a known hijacked spam domain."""
    text = f"{title} {excerpt}"

    if _DECORATIVE_UNICODE.search(text):
        return True

    for domain in SPAM_DOMAINS:
        if domain in source.lower():
            return True

    # Real journalism doesn't repeat "Sheffield Wednesday" (or similar)
    # three-plus times in one headline+excerpt - bot filler text does.
    lower = text.lower()
    if lower.count("sheffield wednesday") >= 3:
        return True

    return False


def fetch_all() -> list[dict]:
    now = time.time()
    articles = []
    for feed in FEEDS:
        try:
            parsed = feedparser.parse(feed["url"], request_headers=FETCH_HEADERS)
        except Exception as e:
            print(f"  [WARN] {feed['source']}: fetch raised {e} — skipping this feed")
            continue

        n_entries = len(parsed.entries)
        if parsed.get("bozo") and n_entries == 0:
            print(f"  [WARN] {feed['source']}: feed errored/empty (bozo={parsed.get('bozo_exception')})")
        else:
            print(f"  [ok] {feed['source']}: {n_entries} entries returned")

        for e in parsed.entries:
            ts = None
            for attr in ("published_parsed", "updated_parsed"):
                if getattr(e, attr, None):
                    ts = time.mktime(getattr(e, attr))
                    break
            if ts is None or (now - ts) > MAX_AGE_DAYS * 86400:
                continue
            title = clean_html(e.get("title", ""))
            # Some feeds scrape the publisher's own page furniture into the
            # headline, e.g. "Real headline Club News | 58 minutes ago".
            # Strip anything from a category label + relative timestamp on.
            title = re.sub(
                r"\s*(Club News|News|Video|Match Report|Interview)\s*\|\s*\d+\s+"
                r"(second|minute|hour|day|week|month)s?\s+ago.*$",
                "", title, flags=re.IGNORECASE,
            ).strip()
            # Google News appends " - Publisher" to titles; strip it on any
            # feed that comes via Google News (incl. official site queries)
            if "news.google.com" in feed["url"]:
                title = re.sub(r"\s+-\s+[^-]+$", "", title)
            excerpt = clean_html(e.get("summary", ""))[:EXCERPT_CHARS]
            if excerpt and title and excerpt.lower().startswith(title.lower()[:50]):
                excerpt = ""
            if not title or not e.get("link"):
                continue
            if is_wrong_club(title, excerpt):
                continue
            if not feed["official"] and is_off_topic(title, excerpt):
                continue
            source = feed["source"] if feed["official"] else real_source(e, feed["source"])
            source = SOURCE_ALIASES.get(source, source)
            if is_stream_spam(title, excerpt, source):
                continue
            articles.append(
                {
                    "title": title,
                    "url": e["link"],
                    "source": source,
                    "published": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "excerpt": excerpt,
                    "official": feed["official"] or source in OFFICIAL_SOURCE_NAMES,
                    "image": extract_image(e),
                }
            )
    return articles


def dedupe(articles: list[dict]) -> list[dict]:
    """Same story from multiple outlets: keep the earliest, drop near-
    duplicate headlines (fuzzy match)."""
    articles.sort(key=lambda a: a["published"])
    kept: list[dict] = []
    for a in articles:
        match = next(
            (i for i, k in enumerate(kept)
             if SequenceMatcher(None, a["title"].lower(), k["title"].lower()).ratio() > 0.75),
            None,
        )
        if match is None:
            kept.append(a)
        elif a.get("official") and not kept[match].get("official"):
            kept[match] = a  # prefer the official version of a duplicate
    kept.sort(key=lambda a: a["published"], reverse=True)
    return kept


def load_previous() -> list[dict]:
    """Last run's articles, filtered to the same age window as fresh
    fetches - used as a fallback if this run's fetch is partial (e.g. a
    feed gets temporarily rate-limited/blocked). Missing/corrupt file or
    a bad timestamp just drops that entry, never fatal."""
    try:
        old = json.loads(OUT.read_text())
    except Exception:
        return []
    now = time.time()
    kept = []
    for a in old:
        try:
            ts = datetime.fromisoformat(a["published"]).timestamp()
        except Exception:
            continue
        if (now - ts) <= MAX_AGE_DAYS * 86400:
            kept.append(a)
    return kept


def load_desc_cache() -> dict:
    """URL -> description. Missing/corrupt file just means starting fresh."""
    try:
        return json.loads(DESC_CACHE.read_text())
    except Exception:
        return {}


def prune_desc_cache(cache: dict, articles: list[dict]) -> dict:
    """Drop cached descriptions for articles no longer in the feed, so the
    file can't grow forever. Articles age out after MAX_AGE_DAYS anyway,
    so anything not in the current set is genuinely gone."""
    live_urls = {a["url"] for a in articles}
    return {url: desc for url, desc in cache.items() if url in live_urls}


def fetch_description(url: str) -> str | None:
    """The publisher's own og:description - the summary they write for
    link previews. Returns None on any failure; the card just shows the
    headline alone, exactly as it does today."""
    try:
        r = requests.get(url, headers=FETCH_HEADERS, timeout=DESC_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return None
        html_text = r.text[:200_000]  # cap: no need to scan a huge page
        for pattern in (
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        ):
            m = re.search(pattern, html_text, flags=re.IGNORECASE)
            if m:
                desc = clean_html(m.group(1)).strip()
                if len(desc) > 20:  # ignore uselessly short/placeholder text
                    return desc[:DESC_MAX_CHARS]
        return None
    except Exception:
        return None


def add_descriptions(articles: list[dict]) -> None:
    """Fill in descriptions from cache, fetching a capped number of new
    ones per run. Mutates articles in place."""
    cache = load_desc_cache()
    cache = prune_desc_cache(cache, articles)

    fetched = 0
    for a in articles:
        url = a["url"]
        if url in cache:
            a["description"] = cache[url] or ""
            continue
        if a.get("excerpt"):
            continue          # already has a real summary, don't waste a fetch
        if fetched >= DESC_MAX_NEW_PER_RUN:
            continue          # cap reached; it'll be picked up next run
        desc = fetch_description(url)
        cache[url] = desc or ""   # cache failures too, so we don't retry forever
        a["description"] = desc or ""
        fetched += 1

    try:
        DESC_CACHE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        print(f"  [WARN] could not write description cache: {e}")
    print(f"  descriptions: {fetched} newly fetched, {len(cache)} cached total")


def main() -> None:
    fresh = fetch_all()
    previous = load_previous()
    # Merge rather than replace: if a feed returned nothing this run
    # (temporary block/rate-limit), its recent stories from last run's
    # data are still here to fall back on rather than the site briefly
    # losing content. dedupe() collapses duplicates and MAX_AGE_DAYS
    # (applied during fetch) keeps genuinely old stories from lingering.
    articles = dedupe(fresh + previous)
    add_descriptions(articles)
    OUT.write_text(json.dumps(articles, indent=2))
    print(f"Fetched {len(fresh)} fresh, {len(previous)} carried over, {len(articles)} total -> {OUT.name}")


if __name__ == "__main__":
    main()
