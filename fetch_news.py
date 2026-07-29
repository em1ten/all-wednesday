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

# Normalise the scruffy names Google News reports so the source list
# stays tidy (one chip per outlet, proper names not domains).
SOURCE_ALIASES = {
    "thestar.co.uk": "The Star",
    "Sheffield Star": "The Star",
    "BBC Sport": "BBC",
    "portsmouth.co.uk": "The News (Portsmouth)",
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
            # Google News appends " - Publisher" to titles; strip it on any
            # feed that comes via Google News (incl. official site queries)
            if "news.google.com" in feed["url"]:
                title = re.sub(r"\s+-\s+[^-]+$", "", title)
            excerpt = clean_html(e.get("summary", ""))[:EXCERPT_CHARS]
            if not title or not e.get("link"):
                continue
            if is_wrong_club(title, excerpt):
                continue
            if not feed["official"] and is_off_topic(title, excerpt):
                continue
            source = feed["source"] if feed["official"] else real_source(e, feed["source"])
            source = SOURCE_ALIASES.get(source, source)
            articles.append(
                {
                    "title": title,
                    "url": e["link"],
                    "source": source,
                    "published": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "excerpt": excerpt,
                    "official": feed["official"] or source in OFFICIAL_SOURCE_NAMES,
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


def main() -> None:
    articles = dedupe(fetch_all())
    OUT.write_text(json.dumps(articles, indent=2))
    print(f"Fetched {len(articles)} articles -> {OUT.name}")


if __name__ == "__main__":
    main()
