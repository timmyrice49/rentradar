# Setup — two commands, then it runs itself

The automation needs the code somewhere a scheduled cloud session can reach.
This repo is committed and ready; it just needs a remote.

## 1. Create the repo and push

```bash
gh repo create rentradar --private --source=. --push
```

Or without the GitHub CLI:

```bash
git remote add origin git@github.com:<you>/rentradar.git
git push -u origin main
```

## 2. Tell Claude the repo URL

Claude creates the daily manager task pointing at it. That task follows
`MANAGER.md`, which is the standing brief — worth reading, since it defines
what the agent may and may not do on its own.

## What starts happening

- **Every 15 minutes** — GitHub Actions runs the crawl, dispatches alerts and
  records lead time. Free on public repos; on a private repo it uses Actions
  minutes, so make it public or expect to pay for the cadence.
- **On every push** — the four test suites run.
- **Once a day** — the manager agent checks for broken sources, repairs them,
  works the discovery queue, opens a PR, and sends you a digest.

## Alerts

`rentradar/alerts.py` ships with a console dispatcher. Point `Dispatcher.sink`
at your channel — Telegram, Pushover, Postmark, Twilio are all a ten-line
`send`. Until you do, alerts are logged rather than delivered.

## Optional credentials

| Secret | Enables |
|---|---|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | the `reddit_nyc` source (register a script app at reddit.com/prefs/apps) |
| Socrata `app_token` in `sources.yaml` | lifts the anonymous rate limit on Housing Connect |

## The one thing to do yourself

Wire `leadtime.record_aggregator_hit` to something real. Everything else in
this system is scaffolding for that number: for each unit found upstream,
when did it surface publicly? Until that is fed, `cli stats` will keep
reporting `measured 0`, and the central claim stays untested.
