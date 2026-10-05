# The Wednesday Times

Every Sheffield Wednesday headline in one clean, fast, **ad-free** feed:
https://thewednesdaytimes.uk

Headlines and short excerpts only — every link goes to the original
publisher, so their traffic and ad revenue stay theirs. Newest first,
nothing ranked; anything left out is counted on `/status.html`.

## How it works

```
restore_state.py  -> downloads the last published articles/fixtures/descriptions
                     from the live site (each Actions run starts from scratch)
fetch_news.py     -> pulls the RSS feeds, filters junk (Sheffield United,
                     off-topic, streaming spam, betting promos), dedupes,
                     merges with last run, writes articles.json + status.json
fetch_fixtures.py -> next match, live score, last result and form from the
                     BBC's monthly fixtures pages, writes fixtures.json
build_site.py     -> index.html, status.html, feed.xml, sitemap.xml,
                     robots.txt, manifest.webmanifest, version.json
```

`.github/workflows/update.yml` runs all of that and deploys to GitHub Pages.
cron-job.org triggers it every 15 minutes; GitHub's own schedule is a backup.
If there are too few stories to build a sensible page, the build stops and the
live site keeps its last good version.

## Run locally

```bash
pip install -r requirements.txt
python3 fetch_news.py
python3 fetch_fixtures.py
python3 build_site.py
open index.html
```

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

The Tests workflow runs these whenever code changes. They use saved copies of
the feeds in `tests/fixtures/`, so they never touch the network.

## Adding sources

Add feeds to `FEEDS` in `fetch_news.py`. Set `"official": True` only for the
club/league. Feeds that come via Google News get their publisher name from
Google; direct feeds give better summaries and pictures.

## The rules that keep it clean and legal

- Headlines + short excerpts + links out. Never full article text,
  never stripping ads off publishers' pages.
- One "support" link, no ad networks.
- The footer disclaims any affiliation with the club.
