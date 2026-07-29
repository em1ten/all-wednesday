# The Wednesday Times

Every Sheffield Wednesday headline in one clean, fast, **ad-free** feed.
Headlines and short excerpts only — every link goes straight to the
original publisher, so their traffic and ad revenue stay theirs.

## How it works

```
fetch_news.py   -> pulls RSS feeds (BBC SWFC + Google News query),
                   dedupes near-identical stories, writes articles.json
build_site.py   -> renders articles.json into index.html (static, no server)
```

No database, no backend, no hosting costs.

## Run locally

```bash
pip install feedparser
python3 fetch_news.py     # needs internet; replaces the sample articles.json
python3 build_site.py
open index.html           # or just double-click it
```

## Deploy free (GitHub Pages, auto-updating)

1. Create a GitHub repo and push this folder to it
2. Repo Settings → Pages → Source: **GitHub Actions**
3. Done. The included workflow (`.github/workflows/update.yml`) refetches
   the news and redeploys **every 30 minutes**, free, forever.
   Your site lives at `https://<yourusername>.github.io/<repo>/`
4. Later: buy a domain (~£10/yr) and point it at Pages for a proper URL

## Adding sources

Open `fetch_news.py` and add feeds to the `FEEDS` list. Good candidates:
The Star's SWFC section, Yorkshire Live, fan sites with RSS. The Google
News query already catches most outlets, but direct feeds are faster and
cleaner. Anything without RSS can wait for v2 (page scraping — check each
site's terms first).

## The rules that keep it clean and legal

- Headlines + short excerpts + links out. Never full article text,
  never stripping ads off publishers' pages.
- One "support" link, no ad networks. If you add a sponsor later, one
  clearly-labelled slot, hand-picked.
- The footer disclaims any affiliation with the club.





## New in this version

- **Dark mode** — masthead toggle; follows system preference by default
- **Official filter** — club site, official YouTube, EFL feeds with badges
- **Fixtures strip** — next match, last result, league position. Free key
  from https://www.football-data.org/client/register — add it as a repo
  secret named `FOOTBALL_DATA_API_KEY` (Settings → Secrets → Actions) and
  the workflow picks it up automatically. Without a key the strip shows
  sample data.
- **Story tags** — Transfers / Injuries / Match / Youth / Club news, auto-
  detected from headlines; tune keywords in TAG_RULES in build_site.py
- **Search** — filters headlines as you type, combines with any filter
- **Your own RSS feed** — feed.xml, so people can follow in a reader
- **Social link previews** — OG tags + share.png. Set SITE_URL in
  build_site.py to your real URL after deploying.
