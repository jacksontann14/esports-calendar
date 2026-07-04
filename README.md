# My Esports Calendar

Subscribable LoL / VALORANT match calendars, auto-refreshed on a schedule via
GitHub Actions and served as static `.ics` files via GitHub Pages.

## Setup (one-time)

1. **Create a new GitHub repo** and push this folder's contents to it
   (repo can be public or private — GitHub Pages needs public on free plans,
   or Pages via a private repo if you're on GitHub Pro/Team/Enterprise).

2. **Get a VALORANT API key** (only needed if you use `game: "valorant"`
   entries in `calendars.yaml`):
   - Go to https://api.henrikdev.xyz/dashboard/
   - Join their linked Discord (required)
   - Generate a free key
   - Open `scraper.py` and replace `YOUR_HENRIKDEV_API_KEY` with your real key
   - **Do not commit a real key to a public repo.** For a public repo, use a
     GitHub Actions secret instead (Settings → Secrets and variables →
     Actions → New repository secret, name it `VALORANT_API_KEY`), and change
     `scraper.py` to read it via `os environ`:
     ```python
     import os
     VALORANT_API_KEY = os.environ.get("VALORANT_API_KEY", "YOUR_HENRIKDEV_API_KEY")
     ```
     then add to `.github/workflows/update.yml` under the "Generate calendars" step:
     ```yaml
     env:
       VALORANT_API_KEY: ${{ secrets.VALORANT_API_KEY }}
     ```

3. **Enable GitHub Pages**:
   - Repo Settings → Pages
   - Source: "Deploy from a branch"
   - Branch: `main`, folder: `/docs`
   - Save. GitHub will give you a URL like:
     `https://yourusername.github.io/my-esports-calendar/`

4. **Update `docs/index.html`**: replace every
   `REPLACE_WITH_YOUR_USERNAME.github.io/my-esports-calendar` with your
   actual GitHub Pages URL (from step 3).

5. **Run the workflow once manually** to generate the first set of `.ics`
   files: repo → Actions tab → "Update calendars" → "Run workflow".

## Day-to-day usage

- **Add a new calendar**: append a new block to `calendars.yaml`, commit,
  push. The scheduled workflow (or the "push" trigger) will build it.
- **Change filters on an existing calendar**: edit its block in
  `calendars.yaml`. Same output URL, updated content on the next run.
- **Subscribe on iPhone**: visit your GitHub Pages URL in Safari on your
  phone and tap a calendar link — it'll trigger "Subscribe to Calendar"
  automatically. Or manually: Settings → Calendar → Accounts → Add Account
  → Other → Add Subscribed Calendar, paste the `webcal://...` URL.
- **Subscribe on Google Calendar**: "Other calendars" → "+" → "From URL",
  paste the `https://...` version of the link (Google doesn't use the
  `webcal://` scheme).
- **Run locally / test before pushing**:
  ```bash
  pip install -r requirements.txt
  python generate.py
  ```

## Notes

- GitHub Actions free tier gives 2,000 minutes/month — this job takes
  seconds per run, so even hourly runs are far under the limit.
- Apple/Google poll subscribed calendars on their own schedule (not
  instant) — expect updates to appear within a few hours of a change,
  not immediately.
- `exclude_tbd: true` will hide undetermined bracket slots (e.g.
  playoff matches where teams aren't decided yet) — if you want those
  slots to later fill in with real team names as they're confirmed,
  leave `exclude_tbd: false` so they stay in the file and update in place.
