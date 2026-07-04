"""
Reads calendars.yaml and generates every configured .ics file, plus a
calendars.json manifest that docs/index.html reads to render subscribe
cards automatically — add/remove a calendar by editing calendars.yaml only.
"""

import json
import os
import sys

import yaml

from scraper import build_calendar, get_events, save_calendar

CONFIG_PATH = "calendars.yaml"
MANIFEST_PATH = "docs/calendars.json"

# Keys that get passed to get_events() vs build_calendar() respectively.
EVENT_FILTER_KEYS = {
    "regions", "team", "days_ahead", "date_start", "date_end",
    "states", "exclude_tbd",
}
CALENDAR_BUILD_KEYS = {
    "cal_name", "text_header", "include_league_tag", "add_reminder_minutes",
}

# Default accent color per game, used by the webpage — override per-calendar
# in calendars.yaml with a "color" key (any valid CSS color) if you want.
DEFAULT_COLORS = {
    "lol": "cyan",
    "valorant": "violet",
}


def main():
    with open(CONFIG_PATH) as f:
        configs = yaml.safe_load(f)

    if not configs:
        print("No calendars defined in calendars.yaml — nothing to do.")
        return

    manifest = []
    had_error = False

    for cfg in configs:
        name = cfg.get("name", "(unnamed)")
        print(f"\n=== Building: {name} ===")

        try:
            game = cfg["game"]
            output = cfg["output"]

            os.makedirs(os.path.dirname(output), exist_ok=True)

            event_kwargs = {k: v for k, v in cfg.items() if k in EVENT_FILTER_KEYS}
            build_kwargs = {k: v for k, v in cfg.items() if k in CALENDAR_BUILD_KEYS}

            events = get_events(game, **event_kwargs)
            print(f"  Fetched {len(events)} matching events")

            cal = build_calendar(events, **build_kwargs)
            save_calendar(cal, output)

            # Path relative to docs/, since that's what gets served — e.g.
            # "docs/lck.ics" -> "lck.ics"
            relative_path = os.path.relpath(output, "docs")

            # Prefer the real league name(s) actually present in the fetched
            # events (e.g. "LCK", "MSI") over the raw `regions` filter string,
            # since the former is what a person recognizes on the card.
            league_names = sorted({ev["league"] for ev in events if ev.get("league")})
            if league_names:
                league_label = " / ".join(league_names)
            elif cfg.get("regions"):
                # No events matched (e.g. off-season) — fall back to whatever
                # was configured so the card isn't blank.
                regions_cfg = cfg["regions"]
                league_label = ", ".join(regions_cfg) if isinstance(regions_cfg, list) else regions_cfg
            else:
                league_label = ""

            manifest.append({
                "name": cfg.get("cal_name", name),
                "game": game,
                "league": league_label,
                "file": relative_path,
                "description": cfg.get("description", f"{len(events)} matches"),
                "color": cfg.get("color", DEFAULT_COLORS.get(game, "cyan")),
            })

        except Exception as exc:
            had_error = True
            print(f"  ERROR building '{name}': {exc}", file=sys.stderr)

    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nWrote manifest with {len(manifest)} calendars to {MANIFEST_PATH}")

    if had_error:
        # Non-zero exit so a failing calendar shows up as a failed GitHub
        # Actions run rather than silently succeeding.
        sys.exit(1)


if __name__ == "__main__":
    main()
