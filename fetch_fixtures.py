#!/usr/bin/env python3
"""
All Wednesday — fixtures, results and league position.

Uses football-data.org (free tier covers the Championship).
Get a free API key: https://www.football-data.org/client/register
Set env var FOOTBALL_DATA_API_KEY.

Without a key this script leaves the existing fixtures.json alone
(the bundled sample keeps the site rendering), so the build never breaks.

Run:  FOOTBALL_DATA_API_KEY=xxx python3 fetch_fixtures.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

OUT = Path(__file__).parent / "fixtures.json"
BASE = "https://api.football-data.org/v4"
COMPETITION = "ELC"  # Championship code. NOTE: SWFC start 2026/27 in
                     # League One, which football-data.org's FREE tier does
                     # NOT cover — so this will report "team not found"
                     # until they return to the Championship. Options now:
                     # keep the strip on sample data, hide it, or replace
                     # this script with a BBC fixtures-page scraper (ask
                     # Claude to build that as a next step).
TEAM_NAME_MATCH = "Sheffield Wednesday"


def main() -> None:
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        print("No FOOTBALL_DATA_API_KEY set — keeping existing fixtures.json")
        sys.exit(0)

    h = {"X-Auth-Token": key}

    # Find the team id by name (avoids hardcoding a wrong id)
    teams = requests.get(f"{BASE}/competitions/{COMPETITION}/teams", headers=h, timeout=30).json()
    team = next((t for t in teams.get("teams", []) if TEAM_NAME_MATCH.lower() in t["name"].lower()), None)
    if not team:
        print(f"Team '{TEAM_NAME_MATCH}' not found in {COMPETITION} — check COMPETITION setting")
        sys.exit(0)
    tid = team["id"]

    # Next fixture + last result
    matches = requests.get(f"{BASE}/teams/{tid}/matches?limit=20", headers=h, timeout=30).json()
    nxt, last = None, None
    for m in matches.get("matches", []):
        entry = {
            "home": m["homeTeam"]["shortName"] or m["homeTeam"]["name"],
            "away": m["awayTeam"]["shortName"] or m["awayTeam"]["name"],
            "date": m["utcDate"],
            "competition": m["competition"]["name"],
            "score": None,
        }
        if m["status"] in ("SCHEDULED", "TIMED") and nxt is None:
            nxt = entry
        elif m["status"] == "FINISHED":
            ft = m["score"]["fullTime"]
            entry["score"] = f"{ft['home']}–{ft['away']}"
            last = entry  # keeps overwriting -> most recent finished

    # League position
    standings = requests.get(f"{BASE}/competitions/{COMPETITION}/standings", headers=h, timeout=30).json()
    pos = None
    for table in standings.get("standings", []):
        if table.get("type") != "TOTAL":
            continue
        for row in table.get("table", []):
            if row["team"]["id"] == tid:
                pos = {"position": row["position"], "played": row["playedGames"], "points": row["points"]}

    OUT.write_text(json.dumps({"sample": False, "next": nxt, "last": last, "standing": pos}, indent=2))
    print(f"Wrote fixtures.json (next: {bool(nxt)}, last: {bool(last)}, standing: {bool(pos)})")


if __name__ == "__main__":
    main()
