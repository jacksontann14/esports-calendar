"""
Reads calendars.yaml and generates every configured .ics file.
Run manually with: python generate.py
Run automatically via .github/workflows/update.yml on a schedule.
"""

import os
import sys

import yaml

from scraper import build_calendar, get_events, save_calendar

CONFIG_PATH = "calendars.yaml"

# Keys that get passed to get_events() vs build_calendar() respectively.
EVENT_FILTER_KEYS = {
    "regions", "team", "days_ahead", "date_start", "date_end",
    "states", "exclude_tbd",
}
CALENDAR_BUILD_KEYS = {
    "cal_name", "text_header", "include_league_tag", "add_reminder_minutes",
}


def main():
    with open(CONFIG_PATH) as f:
        configs = yaml.safe_load(f)

    if not configs:
        print("No calendars defined in calendars.yaml — nothing to do.")
        return

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

        except Exception as exc:
            had_error = True
            print(f"  ERROR building '{name}': {exc}", file=sys.stderr)

    if had_error:
        # Non-zero exit so a failing calendar shows up as a failed GitHub
        # Actions run rather than silently succeeding.
        sys.exit(1)


if __name__ == "__main__":
    main()
