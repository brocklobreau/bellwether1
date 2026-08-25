# Bellwether

A rules-based stock research dashboard: a watchlist plus screened day-trade
and growth-investing candidates, scored on technicals, fundamentals, news
sentiment, and insider activity. This is research tooling, not financial
advice, and it never places trades.

## Why this version exists

The original version of this ran inside an AI chat session on a scheduled
"trigger" that was supposed to fire every hour during market hours. It
turned out to fail silently for days at a time with no way to tell from
outside that it had stopped working, and its data came in through an
LLM-summarization step (WebFetch) that occasionally mangled numbers on bulk
requests.

This version fixes both problems by being boring, ordinary infrastructure
instead, run as a single **always-on Render Web Service** (`app.py`):

- **A real internal timer**, not an AI session -- `app.py` starts a
  background thread on boot that refreshes data every 15 minutes for as
  long as the process is running. Nothing about it depends on a Claude
  session staying alive.
- **Real HTTP calls** (`scripts/fmp_client.py`, using `requests`) straight
  to the Financial Modeling Prep API -- no WebFetch/LLM summarization step
  in between to garble a number.
- **A DST-proof market-hours check** (`scripts/refresh.py`,
  `within_market_hours()`) computed from the actual `America/New_York`
  clock, so the twice-a-year clock change never needs a manual fix. Outside
  actual market hours, each cycle is a no-op.
- **A heartbeat file** (`results/heartbeat.json`) written at the start and
  end of every refresh cycle, so a cycle that started and never finished is
  visible instead of silent.
- **`/healthz`** -- a plain liveness endpoint, if you ever want to point an
  external uptime monitor at this (UptimeRobot, Better Uptime, etc.) for a
  push notification the moment the service itself goes down, on top of the
  heartbeat file.

Everything else -- the scoring math in `lib/`, the dashboard layout in
`lib/dashboard.py` -- is unchanged from before.

There's also a free alternative if you'd rather not pay for an always-on
instance: a real GitHub Actions cron job (`.github/workflows/refresh.yml`)
plus a free Render Static Site. See the comments at the top of
`render.yaml` and in `.github/workflows/refresh.yml` for how to switch to
that instead -- functionally the same refresh logic, just triggered by
GitHub's scheduler and served as static files instead of running inside
one long-lived process.

## One-time setup

You'll need two accounts: **GitHub** (free -- hosts the code) and
**Render** (the always-on web service needs Render's cheapest paid plan,
since free web services sleep after 15 minutes idle, which would silently
kill the background refresh thread). Takes about 10 minutes.

### 1. Push this code to GitHub

- Create a new repository at [github.com/new](https://github.com/new).
  Private is fine (recommended) -- nothing here needs to be public.
- From this project's folder:
  ```
  git init
  git add .
  git commit -m "Bellwether: always-on web service, real HTTP fetch"
  git branch -M main
  git remote add origin https://github.com/<your-username>/<your-repo>.git
  git push -u origin main
  ```

### 2. Deploy on Render

- At [dashboard.render.com](https://dashboard.render.com), sign up.
- **New +** -> **Blueprint**, connect your GitHub account, pick this repo.
  Render reads `render.yaml` automatically and proposes a Web Service on
  the `starter` plan (the cheapest that stays running -- do NOT pick Free
  for this, it will sleep and break the refresh timer).
- When prompted for environment variables, set `FMP_API_KEY` to your
  Financial Modeling Prep API key (find it in your account dashboard at
  financialmodelingprep.com if you don't have it handy -- it's the same key
  this project has been using all along). This is set directly in Render,
  never committed to the repo.
- Once deployed, Render gives you a URL like
  `https://bellwether-dashboard.onrender.com` -- that's your permanent
  dashboard link. The page will show a brief "warming up" message on first
  load while the initial data refresh runs (a minute or two), then serve
  the real dashboard from then on, updating in place every 15 minutes.
- (Optional) Add a custom domain under the service's **Settings ->
  Custom Domains** if you have one.

### 3. Test it

- Visit `/healthz` on your new URL -- should return `{"status": "ok", ...}`.
- Check `results/heartbeat.json` (via Render's **Shell** tab, or just watch
  the service logs) after the first cycle to confirm a real refresh
  completed rather than erroring out (bad/expired key, FMP plan limit,
  etc. all show up there in plain language).

## How the refresh works

Once deployed, `app.py`'s background thread refreshes immediately on boot,
then every 15 minutes for as long as the instance is running.
`scripts/refresh.py` checks the actual current time in `America/New_York`
and does nothing (no API calls) outside real market hours (9:30am-4:00pm
Eastern) or on weekends -- so it only ever does real work during the window
that matters, regardless of how often the timer itself ticks.

Each real refresh cycle:
1. Fetches quote, price history, ratios, growth, analyst targets/ratings,
   profile, earnings date, insider filings, and news for every watchlist
   ticker (`watchlist.json`) and the screener universe.
2. Scores everything through the same `lib/` pipeline as before.
3. Screens for day-trade candidates beyond the watchlist (FMP's company
   screener + movers lists, long-only per your standing preference, ranked
   by `day_trade_score`) and growth-investing candidates (top scorers from
   the screener universe).
4. Saves `results/latest.json` + a timestamped snapshot in
   `results/history/`, and regenerates `site/index.html`, which `app.py`
   serves directly -- no redeploy needed for a data update, only for a code
   change.

**Worth knowing:** without an attached Render persistent disk (a separate
paid add-on, **$0.25/GB/month** -- 1GB is plenty), the service's local
filesystem is wiped on every redeploy or restart -- `results/history/`'s
rolling snapshots reset to whatever was last committed to git, so a Track
Record entry that only exists in that day's live snapshots (e.g. a ticker
that flipped into a signal after the last code push) disappears the next
time we ship a change. `results/latest.json` and the dashboard itself
rebuild fresh either way, so only Track Record history is at risk. Add a
disk under the service's **Settings -> Disks** (name `bellwether-data`,
mount path `/opt/render/project/src/results`, size 1GB -- matches the
`disk:` block in `render.yaml`) if you want history to survive restarts.

## Adding portfolio trades

Same as before: edit `portfolio.json` and push, or use the "Add trade" form
directly on the dashboard's Portfolio tab (saved in your browser's own
local storage, so it's per-device and untouched by every redeploy -- see
`RUNBOOK.md` for the full explanation of how the two logs combine).

## Local testing

`python3 scripts/refresh.py` runs the exact same thing the GitHub Action
runs, using `config/fmp_api_key.txt` as a fallback if `FMP_API_KEY` isn't
set in your environment. Useful for checking the scoring/dashboard logic
changes before pushing -- note this sandbox itself has no outbound network
access to actually reach FMP, so a live end-to-end test needs to happen
somewhere with real internet (your own machine, or Render).

`python3 app.py` runs the whole web service locally on port 8000 (set
`PORT` to change it) -- same background-refresh behavior as the deployed
version, just without gunicorn in front of it.
