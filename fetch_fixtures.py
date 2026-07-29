#!/usr/bin/env python3
"""
The Wednesday Times — fixtures & results.

Primary source: the BBC's public fixtures page for the club, which works
whatever division Wednesday are in (football-data.org's free tier doesn't
cover League One). We parse the JSON the BBC embeds in its own page.

Defensive by design: if the page layout changes and parsing fails, we keep
the existing fixtures.json and print why — the site build never breaks.

Run:  python3 fetch_fixtures.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path(__file__).parent / "fixtures.json"
BBC_URL = "https://www.bbc.co.uk/sport/football/teams/sheffield-wednesday/scores-fixtures"
TEAM_MATCH = "sheffield wednesday"
UA = {"User-Agent": "Mozilla/5.0 (compatible; AllWednesday/1.0; fixtures strip)"}


def extract_embedded_json(page: str) -> list[dict]:
    """BBC pages embed their data as JSON in script tags. Grab every JSON
    blob we can find and return the parsed ones."""
    blobs = []
    patterns = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'window\.__INITIAL_DATA__\s*=\s*"(.*?)";</script>',
        r'window\.__INITIAL_DATA__\s*=\s*(\{.*?\});</script>',
    ]
    for pat in patterns:
        for m in re.finditer(pat, page, re.DOTALL):
            raw = m.group(1)
            for candidate in (raw, raw.encode().decode("unicode_escape", errors="ignore")):
                try:
                    blobs.append(json.loads(candidate))
                    break
                except Exception:
                    continue
    return blobs


def walk(node, found: list[dict]) -> None:
    """Recursively find dicts that look like football events (home/away
    team structures) regardless of exactly where BBC nests them."""
    if isinstance(node, dict):
        keys = {k.lower() for k in node.keys()}
        if {"home", "away"} <= keys or {"hometeam", "awayteam"} <= keys:
            found.append(node)
        for v in node.values():
            walk(v, found)
    elif isinstance(node, list):
        for v in node:
            walk(v, found)


def team_name(side) -> str | None:
    if isinstance(side, dict):
        for path in (("name",), ("fullName",), ("shortName",), ("team", "name"), ("name", "full"), ("name", "abbreviation")):
            cur = side
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, str) and cur:
                return cur
    return None


def score_of(side) -> int | None:
    if isinstance(side, dict):
        for k in ("score", "scores", "goals"):
            v = side.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, dict) and isinstance(v.get("fullTime"), int):
                return v["fullTime"]
    return None


def event_datetime(ev: dict) -> str | None:
    for k in ("startDateTime", "kickoffTime", "date", "startTime", "utcKickoff"):
        v = ev.get(k)
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            return v
        if isinstance(v, dict):
            for kk in ("iso", "isoDate", "dateTime"):
                if isinstance(v.get(kk), str):
                    return v[kk]
    return None


def main() -> None:
    try:
        page = requests.get(BBC_URL, headers=UA, timeout=30).text
    except Exception as e:
        print(f"Could not fetch BBC page ({e}) — keeping existing fixtures.json")
        sys.exit(0)

    events = []
    for blob in extract_embedded_json(page):
        walk(blob, events)

    parsed = []
    for ev in events:
        home = ev.get("home") or ev.get("homeTeam")
        away = ev.get("away") or ev.get("awayTeam")
        hn, an = team_name(home), team_name(away)
        dt = event_datetime(ev)
        if not (hn and an and dt):
            continue
        if TEAM_MATCH not in hn.lower() and TEAM_MATCH not in an.lower():
            continue
        hs, as_ = score_of(home), score_of(away)
        parsed.append({
            "home": hn, "away": an, "date": dt,
            "score": f"{hs}\u2013{as_}" if hs is not None and as_ is not None else None,
        })

    if not parsed:
        print("BBC page fetched but no fixtures parsed (layout may have changed) — keeping existing fixtures.json")
        print("Tip: paste this message to Claude along with the Actions log to get the parser updated.")
        sys.exit(0)

    # de-dupe + sort by date
    seen, unique = set(), []
    for m in parsed:
        key = (m["home"], m["away"], m["date"][:10])
        if key not in seen:
            seen.add(key)
            unique.append(m)
    unique.sort(key=lambda m: m["date"])

    now = datetime.now(timezone.utc).isoformat()
    nxt = next((m for m in unique if m["date"] > now and m["score"] is None), None)
    finished = [m for m in unique if m["score"] is not None and m["date"] <= now]
    last = finished[-1] if finished else None

    OUT.write_text(json.dumps({"sample": False, "next": nxt, "last": last, "standing": None}, indent=2))
    print(f"Wrote fixtures.json from BBC (next: {bool(nxt)}, last: {bool(last)}, {len(unique)} matches seen)")


if __name__ == "__main__":
    main()
