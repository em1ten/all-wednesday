#!/usr/bin/env python3
"""
The Wednesday Times — fixtures, results and form.

Source: the BBC's public scores & fixtures pages for the club, which work
whatever division Wednesday are in. We parse the JSON the BBC embeds in
its own page. The plain page only shows the nearest match day, so we ask
for whole months (/scores-fixtures/2026-10): this month and next for the
next fixture, plus last month when we need more results for the form
guide.

Defensive by design: if the pages can't be fetched or parsed, we keep the
existing fixtures.json and say why — the site build never breaks.

Run:  python3 fetch_fixtures.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).parent
OUT = HERE / "fixtures.json"
STATUS = HERE / "status.json"
BBC_BASE = "https://www.bbc.co.uk/sport/football/teams/sheffield-wednesday/scores-fixtures"
TEAM_MATCH = "sheffield wednesday"
UA = {"User-Agent": "Mozilla/5.0 (compatible; TheWednesdayTimes/1.0; fixtures strip)"}
FETCH_TIMEOUT = 20
FETCH_DEADLINE = 40
MAX_BYTES = 5_000_000
FORM_GAMES = 5
MATCH_LENGTH = timedelta(hours=2, minutes=30)   # kick-off to final whistle, generously
NOT_FOR_FORM = ("friendly", "friendlies", "pre-season", "preseason")
UK = ZoneInfo("Europe/London")


def month_url(year: int, month: int) -> str:
    return f"{BBC_BASE}/{year:04d}-{month:02d}"


def shift_month(d: date, months: int) -> tuple[int, int]:
    m = d.month - 1 + months
    return d.year + m // 12, m % 12 + 1


def fetch_page(url: str) -> str:
    """GET with a timeout, an overall deadline and a size cap. Raises on
    any problem."""
    started = time.monotonic()
    with requests.get(url, headers=UA, timeout=FETCH_TIMEOUT, stream=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        body, size = [], 0
        while True:
            chunk = r.raw.read1(64 * 1024, decode_content=True)
            if not chunk:
                break
            body.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise RuntimeError("page bigger than 5 MB")
            if time.monotonic() - started > FETCH_DEADLINE:
                raise RuntimeError(f"download took longer than {FETCH_DEADLINE}s")
        return b"".join(body).decode(r.encoding or "utf-8", errors="replace")


def extract_embedded_json(page: str) -> list[dict]:
    """BBC pages embed their data as JSON in script tags. Grab every JSON
    blob we can find and return the parsed ones."""
    blobs = []
    for m in re.finditer(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.DOTALL):
        try:
            blobs.append(json.loads(m.group(1)))
        except ValueError:
            pass
    # window.__INITIAL_DATA__ = "<JSON encoded as a JS string>";
    for m in re.finditer(r'window\.__INITIAL_DATA__\s*=\s*("(?:[^"\\]|\\.)*")\s*;', page, re.DOTALL):
        try:
            blobs.append(json.loads(json.loads(m.group(1))))
        except ValueError:
            pass
    # window.__INITIAL_DATA__ = {...};
    for m in re.finditer(r'window\.__INITIAL_DATA__\s*=\s*(\{.*?\});</script>', page, re.DOTALL):
        try:
            blobs.append(json.loads(m.group(1)))
        except ValueError:
            pass
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


def _dig(obj, path):
    for k in path:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def team_name(side) -> str | None:
    for path in (("fullName",), ("name",), ("shortName",), ("team", "name"),
                 ("name", "fullName"), ("name", "full"), ("name", "abbreviation")):
        v = _dig(side, path)
        if isinstance(v, str) and v:
            return v
    return None


def short_name(side) -> str | None:
    for path in (("shortName",), ("name", "shortName")):
        v = _dig(side, path)
        if isinstance(v, str) and v:
            return v
    return None


def _as_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def score_of(side) -> int | None:
    """BBC scores have come through as numbers and as strings ("2"),
    under a few different names; accept all of them."""
    if not isinstance(side, dict):
        return None
    for path in (("score",), ("runningScore",), ("scores", "fullTime"), ("fullTimeScore",), ("goals",)):
        v = _as_int(_dig(side, path))
        if v is not None:
            return v
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


def parse_kickoff(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:   # a bare date: treat as 15:00 UK
        dt = dt.replace(hour=15, tzinfo=UK)
    return dt.astimezone(timezone.utc)


def competition_name(ev: dict) -> str | None:
    """Best-effort competition label (e.g. 'Friendly', 'EFL Trophy',
    'League One'). Purely cosmetic."""
    for path in (("tournament", "name"), ("competition", "name"), ("competitionName",),
                 ("league", "name"), ("stage", "competition", "name")):
        v = _dig(ev, path)
        if isinstance(v, str) and v:
            return v
    return None


def event_state(ev: dict) -> str | None:
    """'pre', 'live', 'post', 'off' (postponed/abandoned) or None if the
    page doesn't say."""
    words = json.dumps([ev.get("statusComment"), ev.get("periodLabel")]).lower()
    if any(w in words for w in ("postponed", "cancelled", "abandoned", "suspended")):
        return "off"
    status = str(ev.get("status") or "").lower().replace("_", "").replace("-", "")
    if status in ("postevent", "result", "fulltime", "ft", "finished", "complete"):
        return "post"
    if status in ("midevent", "live", "inprogress", "halftime"):
        return "live"
    if status in ("preevent", "fixture", "scheduled", "notstarted"):
        return "pre"
    return None


def time_known(ev: dict) -> bool:
    certain = _dig(ev, ("time", "timeCertainty"))
    return certain is not False


def parse_events(page: str) -> list[dict]:
    events: list[dict] = []
    for blob in extract_embedded_json(page):
        walk(blob, events)
    parsed = []
    for ev in events:
        home = ev.get("home") or ev.get("homeTeam")
        away = ev.get("away") or ev.get("awayTeam")
        hn, an = team_name(home), team_name(away)
        iso = event_datetime(ev)
        ko = parse_kickoff(iso) if iso else None
        if not (hn and an and ko):
            continue
        if TEAM_MATCH not in hn.lower() and TEAM_MATCH not in an.lower():
            continue
        hs, as_ = score_of(home), score_of(away)
        parsed.append({
            "home": hn, "away": an,
            "home_short": short_name(home) or hn, "away_short": short_name(away) or an,
            "date": ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "time_known": time_known(ev),
            "competition": competition_name(ev),
            "state": event_state(ev),
            "home_score": hs, "away_score": as_,
            "is_home": TEAM_MATCH in hn.lower(),
        })
    return parsed


def _score_text(m: dict) -> str | None:
    if m["home_score"] is None or m["away_score"] is None:
        return None
    return f"{m['home_score']}–{m['away_score']}"


def _public(m: dict) -> dict:
    """The shape build_site.py reads."""
    return {"home": m["home"], "away": m["away"],
            "home_short": m.get("home_short") or m["home"], "away_short": m.get("away_short") or m["away"],
            "date": m["date"],
            "time_known": m["time_known"], "competition": m["competition"],
            "score": _score_text(m), "is_home": m["is_home"]}


def result_letter(m: dict) -> str:
    ours, theirs = ((m["home_score"], m["away_score"]) if m["is_home"]
                    else (m["away_score"], m["home_score"]))
    return "W" if ours > theirs else "L" if ours < theirs else "D"


def summarise(matches: list[dict], now: datetime) -> dict:
    """next fixture, a match in progress, the last result and recent form."""
    seen, unique = set(), []
    for m in sorted(matches, key=lambda m: m["date"]):
        key = (m["home"], m["away"], m["date"][:10])
        if key in seen:
            # a later page may know more (e.g. the final score) - keep the richest copy
            i = next(i for i, u in enumerate(unique) if (u["home"], u["away"], u["date"][:10]) == key)
            if unique[i]["home_score"] is None and m["home_score"] is not None:
                unique[i] = m
            continue
        seen.add(key)
        unique.append(m)

    def kickoff(m):
        return parse_kickoff(m["date"])

    def finished(m):
        if m["state"] == "post":
            return _score_text(m) is not None
        return (m["state"] is None and _score_text(m) is not None
                and kickoff(m) + MATCH_LENGTH <= now)

    def in_play(m):
        if m["state"] == "live":
            return True
        return (m["state"] in (None, "pre") and kickoff(m) <= now < kickoff(m) + MATCH_LENGTH
                and _score_text(m) is not None)

    live = next((m for m in unique if in_play(m)), None)
    nxt = next((m for m in unique if kickoff(m) > now and m["state"] in (None, "pre")), None)
    results = [m for m in unique if finished(m)]
    last = results[-1] if results else None
    competitive = [m for m in results
                   if not any(w in (m["competition"] or "").lower() for w in NOT_FOR_FORM)]
    form = [{"result": result_letter(m), **_public(m)} for m in competitive[-FORM_GAMES:]]
    return {
        "next": _public(nxt) if nxt else None,
        "live": _public(live) if live else None,
        "last": _public(last) if last else None,
        "form": form,
    }


def update_status(info: dict) -> None:
    """Add this run's fixtures result to status.json (written by
    fetch_news.py just before this runs)."""
    try:
        status = json.loads(STATUS.read_text())
        if not isinstance(status, dict):
            status = {}
    except Exception:
        status = {}
    status["fixtures"] = info
    try:
        STATUS.write_text(json.dumps(status, indent=2))
    except Exception:
        pass


def warn(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=The Wednesday Times::{message}")
    else:
        print(f"  [WARN] {message}")


def main(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    today_uk = now.astimezone(UK).date()
    pages = []
    matches: list[dict] = []

    def load(url: str) -> None:
        try:
            found = parse_events(fetch_page(url))
            pages.append({"url": url, "ok": True, "matches": len(found)})
            matches.extend(found)
        except Exception as e:
            pages.append({"url": url, "ok": False, "error": (str(e) or type(e).__name__)[:200]})

    for months in (0, 1):
        load(month_url(*shift_month(today_uk, months)))
    summary = summarise(matches, now)
    if len(summary["form"]) < FORM_GAMES or not summary["last"]:
        load(month_url(*shift_month(today_uk, -1)))
    if not matches:
        load(BBC_BASE)   # the plain page, in case the month pages ever change
    summary = summarise(matches, now)

    info = {"checked": now.isoformat(timespec="seconds"), "pages": pages,
            "matches_found": len(matches)}
    if not matches:
        info["ok"] = False
        info["error"] = "no fixtures found on the BBC pages - kept the previous fixtures.json"
        update_status(info)
        warn("BBC fixtures: nothing parsed (page down or layout changed) - keeping existing fixtures.json")
        return 0

    info["ok"] = True
    update_status(info)
    out = {"sample": False, **summary, "standing": None,
           "updated": now.isoformat(timespec="seconds")}
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, OUT)
    print(f"Wrote fixtures.json from BBC (next: {bool(summary['next'])}, live: {bool(summary['live'])}, "
          f"last: {bool(summary['last'])}, form: {''.join(f['result'] for f in summary['form']) or '-'}, "
          f"{len(matches)} matches seen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
