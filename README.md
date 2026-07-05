# Match Schedule Hub

Subscribable calendars for League of Legends and VALORANT esports —
LCK, MSI, VCT Pacific, your favorite team, whatever you want. Pick a
calendar, tap subscribe, matches show up automatically. No app, no login.

## How it works

- A daily GitHub Action pulls the latest schedule from the LoL Esports and
  VALORANT (HenrikDev) APIs
- Each configured calendar is exported as a `.ics` file and published via
  GitHub Pages
- Anyone can subscribe from Apple Calendar, Google Calendar, or Outlook
  with one tap — the feed updates itself, no re-downloading required

## Calendars

Live list and one-tap subscribe links: **`https://<your-username>.github.io/<repo>/`**

## Adding or changing a calendar

Everything is config-driven — edit `calendars.yaml`, commit, push. No code
or HTML changes needed; the webpage and every `.ics` file rebuild
automatically on the next scheduled run.

```yaml
- name: "T1 Game Day"
  output: "docs/t1_schedule.ics"
  game: "lol"
  team: "T1"
  days_ahead: 30
  exclude_tbd: true
  cal_name: "T1 Schedule"
  description: "Every T1 match, next 30 days"
  text_header: "T1 Game Day"
  include_league_tag: false
  add_reminder_minutes: 30
```

For first-time setup, deployment, and troubleshooting, see `SETUP.md`
(not tracked in this repo — see below).

## Credit

Built on public schedule data from LoL Esports and HenrikDev's VALORANT
API. Not affiliated with or endorsed by Riot Games.
