#!/usr/bin/env python3
"""
All Wednesday — feed fetcher.

Pulls Sheffield Wednesday headlines from public RSS feeds, dedupes them,
and writes articles.json for the site builder.

We only store headline + short excerpt + link out to the source — we never
republish article content. That keeps it clean legally and sends readers
(and the ad revenue) to the publishers.

Run:  python3 fetch_news.py
"""

from __future__ import annotations

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
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=REPLACE_WITH_CHANNEL_ID",
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
EXCERPT_CHARS = 180
OUT = Path(__file__).parent / "articles.json"


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def real_source(entry, fallback: str) -> str:
    """Google News entries carry the actual publisher in entry.source."""
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return src.title
    return fallback


def fetch_all() -> list[dict]:
    now = time.time()
    articles = []
    for feed in FEEDS:
        parsed = feedparser.parse(feed["url"])
        for e in parsed.entries:
            ts = None
            for attr in ("published_parsed", "updated_parsed"):
                if getattr(e, attr, None):
                    ts = time.mktime(getattr(e, attr))
                    break
            if ts is None or (now - ts) > MAX_AGE_DAYS * 86400:
                continue
            title = clean_html(e.get("title", ""))
            # Google News appends " - Publisher" to titles; strip it
            title = re.sub(r"\s+-\s+[^-]+$", "", title) if feed["source"] == "Google News" else title
            excerpt = clean_html(e.get("summary", ""))[:EXCERPT_CHARS]
            if not title or not e.get("link"):
                continue
            articles.append(
                {
                    "title": title,
                    "url": e["link"],
                    "source": feed["source"] if feed["official"] else real_source(e, feed["source"]),
                    "published": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "excerpt": excerpt,
                    "official": feed["official"],
                }
            )
    return articles


def dedupe(articles: list[dict]) -> list[dict]:
    """Same story from multiple outlets: keep the earliest, drop near-
    duplicate headlines (fuzzy match)."""
    articles.sort(key=lambda a: a["published"])
    kept: list[dict] = []
    for a in articles:
        dup = any(
            SequenceMatcher(None, a["title"].lower(), k["title"].lower()).ratio() > 0.75
            for k in kept
        )
        if not dup:
            kept.append(a)
    kept.sort(key=lambda a: a["published"], reverse=True)
    return kept


def main() -> None:
    articles = dedupe(fetch_all())
    OUT.write_text(json.dumps(articles, indent=2))
    print(f"Fetched {len(articles)} articles -> {OUT.name}")


if __name__ == "__main__":
    main()
