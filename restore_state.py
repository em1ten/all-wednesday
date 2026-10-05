#!/usr/bin/env python3
"""
The Wednesday Times — restore last run's data.

Each GitHub Actions run starts from a fresh checkout of the repo, so the
articles.json / descriptions.json / fixtures.json that the previous run
produced are gone - the copies in the repo are old samples. Without this
step, a run where Google News blocks us would simply lose those stories.

The previous run's files are on the live site, so we download them from
there before fetching. Each file is checked before it replaces the local
copy; anything missing, unreachable or malformed is skipped and the
local file stays. This step can't fail the build.

Run:  python3 restore_state.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from build_site import SITE_URL

HERE = Path(__file__).parent
TIMEOUT = 15
MAX_BYTES = 5_000_000
UA = {"User-Agent": "TheWednesdayTimes-build/1.0 (+restore last run)"}


def _valid_articles(data) -> bool:
    if not isinstance(data, list):
        return False
    for a in data:
        if not (isinstance(a, dict) and isinstance(a.get("title"), str)
                and isinstance(a.get("url"), str) and isinstance(a.get("source"), str)
                and isinstance(a.get("published"), str)):
            return False
        try:
            datetime.fromisoformat(a["published"])
        except ValueError:
            return False
    return True


def _valid_descriptions(data) -> bool:
    return isinstance(data, dict) and all(isinstance(k, str) for k in data)


def _valid_fixtures(data) -> bool:
    return (isinstance(data, dict) and data.get("sample") is False
            and all(k in data for k in ("next", "last")))


FILES = {
    "articles.json": _valid_articles,
    "descriptions.json": _valid_descriptions,
    "fixtures.json": _valid_fixtures,
}


def fetch_json(name: str):
    url = f"{SITE_URL}/{name}?restore={int(time.time())}"   # dodge any cache
    started = time.monotonic()
    with requests.get(url, headers=UA, timeout=TIMEOUT, stream=True) as r:
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
                raise RuntimeError("file bigger than 5 MB")
            if time.monotonic() - started > TIMEOUT * 2:
                raise RuntimeError("download too slow")
    return json.loads(b"".join(body).decode("utf-8"))


def main() -> int:
    for name, valid in FILES.items():
        try:
            data = fetch_json(name)
        except Exception as e:
            print(f"  [skip] {name}: couldn't download ({str(e)[:120]}) - keeping local copy")
            continue
        if not valid(data):
            print(f"  [skip] {name}: downloaded copy didn't look right - keeping local copy")
            continue
        path = HERE / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)
        size = len(data) if isinstance(data, (list, dict)) else 0
        print(f"  [ok] {name}: restored from the live site ({size} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
