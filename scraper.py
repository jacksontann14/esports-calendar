"""
Esports schedule toolkit — LoL + VALORANT
Fetches match schedules, normalizes them into a common format, filters,
and exports to .ics calendar files.
"""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from icalendar import Alarm, Calendar, Event

# ============================================================
# CONFIG
# ============================================================

LOL_API_KEY = os.environ.get("LOL_API_KEY", "YOUR_HENRIKDEV_API_KEY")
LOL_BASE_URL = "https://esports-api.lolesports.com/persisted/gw"
LOL_HEADERS = {"x-api-key": LOL_API_KEY}

# Get your own free key at https://api.henrikdev.xyz/dashboard (requires joining their Discord)
VALORANT_API_KEY = os.environ.get("VALORANT_API_KEY", "YOUR_HENRIKDEV_API_KEY")

VALORANT_BASE_URL = "https://api.henrikdev.xyz/valorant/v1/esports/schedule"
VALORANT_HEADERS = {"Authorization": VALORANT_API_KEY}

SUPPORTED_GAMES = {"lol", "valorant"}

# VALORANT's unfiltered schedule call is truncated/capped by HenrikDev — it's
# only used to discover which league identifiers are currently live, never
# as a source of a complete match list. Passing an exact `league` param
# returns that league's full schedule instead. Cache both separately.
_valorant_cache = {"unfiltered": None, "by_league": {}}


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _parse_iso(timestamp):
    """Parse ISO 8601 timestamps whether they end in 'Z' or '+00:00'."""
    timestamp = timestamp.replace("Z", "+00:00")
    dt = datetime.fromisoformat(timestamp)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(text):
    """Lowercase and strip spaces/underscores, for loose string matching."""
    return re.sub(r"[\s_]+", "", (text or "").lower())


def _estimate_duration(format_str):
    """Rough match duration based on best-of count in the format string."""
    match = re.search(r"(\d+)", format_str or "")
    count = int(match.group(1)) if match else 3
    durations = {1: timedelta(hours=1), 3: timedelta(hours=2), 5: timedelta(hours=3)}
    return durations.get(count, timedelta(hours=1, minutes=30))


def _to_utc(dt):
    """Ensure a datetime is timezone-aware in UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ============================================================
# RAW FETCHERS (private — not called directly by users)
# ============================================================

def _lol_get_leagues():
    resp = requests.get(f"{LOL_BASE_URL}/getLeagues", headers=LOL_HEADERS, params={"hl": "en-US"})
    resp.raise_for_status()
    return resp.json()["data"]["leagues"]


def _lol_get_teams():
    resp = requests.get(f"{LOL_BASE_URL}/getTeams", headers=LOL_HEADERS, params={"hl": "en-US"})
    resp.raise_for_status()
    return resp.json()["data"]["teams"]


def _lol_find_league_ids(region_names):
    region_names = [region_names] if isinstance(region_names, str) else region_names
    leagues = _lol_get_leagues()
    matched = []
    for region in region_names:
        hit = next(
            (lg for lg in leagues if region.lower() in lg["name"].lower() or region.lower() == lg["slug"].lower()),
            None,
        )
        if not hit:
            raise ValueError(f"[lol] No league found matching '{region}'")
        matched.append(hit["id"])
    return matched


def _lol_raw_schedule(league_ids=None):
    events, page_token = [], None
    params = {"hl": "en-US"}
    if league_ids:
        params["leagueId"] = league_ids
    while True:
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(f"{LOL_BASE_URL}/getSchedule", headers=LOL_HEADERS, params=params)
        resp.raise_for_status()
        schedule = resp.json()["data"]["schedule"]
        events.extend(schedule["events"])
        older_token = schedule.get("pages", {}).get("older")
        if not older_token or older_token == page_token:
            break
        page_token = older_token
    return events


def _valorant_discover(force_refresh=False):
    """
    Fetch the unfiltered schedule — HenrikDev truncates/caps this response,
    so it's used ONLY to discover which league identifiers are currently
    live, never as a source of a complete match list for any single league.
    """
    if _valorant_cache["unfiltered"] is not None and not force_refresh:
        return _valorant_cache["unfiltered"]

    resp = requests.get(VALORANT_BASE_URL, headers=VALORANT_HEADERS)
    if resp.status_code == 401:
        raise RuntimeError(
            "VALORANT API returned 401 Unauthorized — check VALORANT_API_KEY is a "
            "real key from https://api.henrikdev.xyz/dashboard/, that "
            "VALORANT_HEADERS was rebuilt after setting it, and check "
            "https://status.henrikdev.xyz/ for outages."
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    _valorant_cache["unfiltered"] = data
    return data


def _valorant_fetch_league(identifier, force_refresh=False):
    """
    Fetch the FULL schedule for one exact league identifier (e.g.
    'vct_pacific'). Passing this param to HenrikDev returns that league's
    complete schedule instead of the capped default — this is what avoids
    the truncation bug, as long as `identifier` is the exact string
    HenrikDev expects (see _valorant_resolve_identifiers below).

    If the identifier isn't recognized (400 Bad Request — common for
    one-off international events like Masters/Champions, which are often
    tagged per-edition rather than as a stable slug), this prints a
    warning and returns an empty list instead of raising, so one bad
    identifier in a multi-region call doesn't wipe out the rest.
    """
    if not force_refresh and identifier in _valorant_cache["by_league"]:
        return _valorant_cache["by_league"][identifier]

    resp = requests.get(VALORANT_BASE_URL, headers=VALORANT_HEADERS, params={"league": identifier})
    if resp.status_code == 401:
        raise RuntimeError(
            "VALORANT API returned 401 Unauthorized — check VALORANT_API_KEY is a "
            "real key from https://api.henrikdev.xyz/dashboard/."
        )
    if resp.status_code == 400:
        print(
            f"Warning: VALORANT API rejected league identifier '{identifier}' "
            f"(400 Bad Request) — skipping it. This usually means it isn't an "
            f"exact identifier HenrikDev recognizes. One-off international "
            f"events (Masters, Champions) are often tagged per-edition rather "
            f"than a stable generic name — check _valorant_discover() output "
            f"while the event is actually live to find the real identifier."
        )
        _valorant_cache["by_league"][identifier] = []
        return []
    resp.raise_for_status()
    data = resp.json()["data"]
    _valorant_cache["by_league"][identifier] = data
    return data


def _valorant_resolve_identifiers(query, force_refresh=False):
    """
    Loose-match a user-facing query (e.g. 'china', 'pacific', 'emea')
    against whatever league identifiers are CURRENTLY LIVE in the
    (truncated) discovery call, and return the exact identifier string(s)
    HenrikDev expects for the `league` param.

    If nothing matches (e.g. the league exists but has no matches in the
    current discovery window, so it never showed up to match against),
    falls back to treating the query itself as the identifier — this lets
    you pass a known-exact slug like 'vct_pacific' directly even when
    discovery can't confirm it.
    """
    raw = _valorant_discover(force_refresh=force_refresh)
    events = [e for e in (_normalize_valorant_event(r) for r in raw) if e]
    live_identifiers = {ev["identifier"] for ev in events if ev["identifier"]}

    q = _norm(query)
    matches = [ident for ident in live_identifiers if q in _norm(ident)]

    if not matches:
        # Not found in the current discovery sample — best-effort fallback,
        # try the query as a literal identifier rather than failing outright.
        print(
            f"Note: '{query}' wasn't found among currently-live VALORANT "
            f"league identifiers — trying it as a literal identifier. If "
            f"this fails, the league may use a different exact slug (or, "
            f"for one-off events like Masters/Champions, a per-edition name)."
        )
        return [query]
    return matches


# ============================================================
# NORMALIZERS -> unified event dict shape
# ============================================================

def _normalize_lol_event(raw):
    match = raw.get("match")
    if not match:
        return None
    league = raw.get("league", {})
    return {
        "game": "lol",
        "id": match.get("id", str(uuid.uuid4())),
        "start": _parse_iso(raw["startTime"]),
        "league": league.get("name", ""),
        "identifier": league.get("slug", ""),
        "region": league.get("name", ""),
        "teams": [t.get("name", "TBD") for t in match.get("teams", [])] or ["TBD"],
        "format": match.get("strategy", {}).get("type", ""),
        "state": raw.get("state", ""),
    }


def _normalize_valorant_event(raw):
    match = raw.get("match")
    if not match:
        return None
    league = raw.get("league", {})
    game_type = match.get("game_type", {})
    return {
        "game": "valorant",
        "id": match.get("id", str(uuid.uuid4())),
        "start": _parse_iso(raw["date"]),
        "league": league.get("name", ""),
        "identifier": league.get("identifier", ""),
        "region": league.get("region", ""),
        "teams": [t.get("name", "TBD") for t in match.get("teams", [])] or ["TBD"],
        "format": f"{game_type.get('type', '')} {game_type.get('count', '')}".strip(),
        "state": raw.get("state", ""),
    }


def _matches_region_query(event, query):
    q = _norm(query)
    return any(q in _norm(field) for field in (event["league"], event["identifier"], event["region"]))


# ============================================================
# PUBLIC API
# ============================================================

def list_regions(game, force_refresh=False):
    """
    Discovery function: list available region/league identifiers.
    LoL: authoritative, from the real getLeagues endpoint.
    VALORANT: derived from what's LIVE in the schedule right now —
    off-season leagues won't appear.
    """
    game = game.lower()
    if game == "lol":
        leagues = _lol_get_leagues()
        for lg in sorted(leagues, key=lambda x: x["name"]):
            print(f"{lg['name']:<25} slug={lg['slug']:<20} id={lg['id']}")
        return leagues

    if game == "valorant":
        raw = _valorant_discover(force_refresh=force_refresh)
        events = [e for e in (_normalize_valorant_event(r) for r in raw) if e]
        seen = {ev["identifier"] or ev["league"]: ev["region"] for ev in events}
        print("VALORANT leagues currently live (off-season leagues won't appear):")
        for k, v in sorted(seen.items()):
            print(f"  {k:<30} region={v}")
        return seen

    raise ValueError(f"Unsupported game '{game}'. Choose from {SUPPORTED_GAMES}")


def list_teams(game, region=None, force_refresh=False):
    """
    Discovery function: list team codes/names.
    LoL: real team directory (complete, region-independent).
    VALORANT: derived from the current schedule window only.
    """
    game = game.lower()
    if game == "lol":
        teams = _lol_get_teams()
        if region:
            teams = [t for t in teams if region.lower() in (t.get("homeLeague") or {}).get("name", "").lower()]
        for t in sorted(teams, key=lambda x: x.get("code") or ""):
            print(f"{t.get('code', '???'):<8} {t['name']:<25} region={(t.get('homeLeague') or {}).get('name', 'N/A')}")
        return teams

    if game == "valorant":
        events = get_events("valorant", regions=region, force_refresh=force_refresh)
        seen = {name: ev["region"] for ev in events for name in ev["teams"]}
        for name, reg in sorted(seen.items()):
            print(f"{name:<25} region={reg}")
        return seen

    raise ValueError(f"Unsupported game '{game}'. Choose from {SUPPORTED_GAMES}")


def get_events(
    game,
    regions=None,
    team=None,
    days_ahead=None,
    date_start=None,
    date_end=None,
    states=None,
    exclude_tbd=False,
    force_refresh=False,
):
    """
    One-stop function to fetch and filter match events for either game.

    game:          'lol' or 'valorant'
    regions:       str or list of str — league/region filter.
                   LoL matches against real league names/slugs.
                   VALORANT: loosely matched against currently-live league
                   identifiers (e.g. 'china', 'emea', 'pacific'), then the
                   FULL schedule for each resolved league is fetched
                   directly (avoids HenrikDev's truncated default response).
                   If no live match is found, your query is tried as a
                   literal identifier as a fallback.
    team:          str — only return matches involving this team
                   (substring match, case-insensitive).
                   VALORANT caveat: there's no way to query HenrikDev by
                   team directly, so a team filter with no `regions` still
                   searches the truncated discovery response and can miss
                   matches. Pass `regions` alongside `team` when possible
                   (e.g. team="PRX", regions="pacific") to search that
                   league's full schedule instead.
    days_ahead:    int — shortcut for "from now until N days from now".
    date_start:    datetime — explicit range start (used instead of days_ahead).
    date_end:      datetime — explicit range end.
    states:        str or list of str — filter by match state, e.g. "unstarted".
                   Inspect {ev['state'] for ev in events} first if unsure of
                   the exact values a given API returns.
    exclude_tbd:   bool — drop matches where a team slot is still "TBD"
                   (undetermined bracket slot).
    force_refresh: bool — bypass the VALORANT in-memory cache.

    Returns a deduplicated list of normalized event dicts, sorted by start time.
    """
    game = game.lower()
    if game == "lol":
        league_ids = _lol_find_league_ids(regions) if regions else None
        raw_events = _lol_raw_schedule(league_ids)
        events = [e for e in (_normalize_lol_event(r) for r in raw_events) if e]
    elif game == "valorant":
        if regions:
            queries = [regions] if isinstance(regions, str) else regions
            resolved_identifiers = set()
            for q in queries:
                resolved_identifiers.update(_valorant_resolve_identifiers(q, force_refresh=force_refresh))

            raw_events = []
            for identifier in resolved_identifiers:
                raw_events.extend(_valorant_fetch_league(identifier, force_refresh=force_refresh))

            events = [e for e in (_normalize_valorant_event(r) for r in raw_events) if e]
            # Defensive client-side filter too, in case a resolved identifier's
            # full response ever includes events outside what was asked for.
            events = [ev for ev in events if any(_matches_region_query(ev, q) for q in queries)]
        else:
            # No filter: this is the capped/truncated discovery response —
            # fine for a broad look, but not guaranteed complete for any
            # single league. Filter by `regions` above to get full data.
            raw_events = _valorant_discover(force_refresh=force_refresh)
            events = [e for e in (_normalize_valorant_event(r) for r in raw_events) if e]

    else:
        raise ValueError(f"Unsupported game '{game}'. Choose from {SUPPORTED_GAMES}")

    # --- team filter ---
    if team:
        events = [ev for ev in events if any(team.lower() in t.lower() for t in ev["teams"])]

    # --- date range filter ---
    if days_ahead is not None:
        date_start = datetime.now(timezone.utc)
        date_end = date_start + timedelta(days=days_ahead)
    date_start, date_end = _to_utc(date_start), _to_utc(date_end)
    if date_start:
        events = [ev for ev in events if ev["start"] >= date_start]
    if date_end:
        events = [ev for ev in events if ev["start"] <= date_end]

    # --- state filter ---
    if states:
        wanted = {s.lower() for s in ([states] if isinstance(states, str) else states)}
        events = [ev for ev in events if ev["state"].lower() in wanted]

    # --- TBD filter ---
    if exclude_tbd:
        events = [ev for ev in events if "TBD" not in ev["teams"]]

    # --- dedupe (always on — safe no-op if there are no duplicates) ---
    events = list({ev["id"]: ev for ev in events}.values())

    return sorted(events, key=lambda ev: ev["start"])


def print_schedule(events, local_tz="America/New_York"):
    """Console preview of events in a local timezone, before exporting."""
    tz = ZoneInfo(local_tz)
    for ev in events:
        local_time = ev["start"].astimezone(tz)
        matchup = " vs ".join(ev["teams"])
        print(f"{local_time.strftime('%Y-%m-%d %H:%M %Z'):<25} [{ev['game'].upper()}][{ev['league']}] {matchup}")


def build_calendar(events, cal_name="Esports Schedule", text_header=None,
                    include_league_tag=True, add_reminder_minutes=None):
    """
    Build an icalendar Calendar object from normalized events.

    text_header:          optional string prepended to every summary, e.g.
                          "PRX Game Day" -> "PRX Game Day: [VALORANT][VCT Pacific] PRX vs TL"
    include_league_tag:   if False, drops the "[GAME][LEAGUE]" prefix —
                          combined with text_header gives "PRX Game Day: PRX vs TL"
    add_reminder_minutes: if set (e.g. 30), adds a popup VALARM reminder.
    """
    cal = Calendar()
    cal.add("prodid", "-//esports-scraper//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)

    for ev in events:
        matchup = " vs ".join(ev["teams"])
        base_summary = f"[{ev['game'].upper()}][{ev['league']}] {matchup}" if include_league_tag else matchup
        summary = f"{text_header}: {base_summary}" if text_header else base_summary

        vevent = Event()
        vevent.add("uid", f"{ev['id']}@{ev['game']}-scraper")
        vevent.add("summary", summary)
        vevent.add("dtstart", ev["start"])
        vevent.add("dtend", ev["start"] + _estimate_duration(ev["format"]))
        vevent.add("description", f"Format: {ev['format']}\nState: {ev['state']}\nRegion: {ev['region']}")

        if add_reminder_minutes:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", f"Reminder: {summary}")
            alarm.add("trigger", timedelta(minutes=-add_reminder_minutes))
            vevent.add_component(alarm)

        cal.add_component(vevent)

    return cal


def save_calendar(cal, filename):
    """Write a Calendar object to disk as .ics."""
    with open(filename, "wb") as f:
        f.write(cal.to_ical())
    print(f"Saved {len(cal.subcomponents)} events to {filename}")


def refresh_calendar(filename, game, cal_name="Esports Schedule", text_header=None,
                      include_league_tag=True, add_reminder_minutes=None, **event_filters):
    """
    Re-fetches live data and overwrites an existing .ics file with updated
    event info — e.g. filling in TBD vs TBD slots once brackets are decided,
    or picking up newly announced fixtures.

    filename:      path to the .ics file to (re)write.
    game:          'lol' or 'valorant'.
    event_filters: same keyword filters as get_events() — regions, team,
                   days_ahead, date_start, date_end, states, exclude_tbd.
    """
    event_filters["force_refresh"] = True  # always bypass cache on refresh
    events = get_events(game, **event_filters)
    cal = build_calendar(
        events,
        cal_name=cal_name,
        text_header=text_header,
        include_league_tag=include_league_tag,
        add_reminder_minutes=add_reminder_minutes,
    )
    save_calendar(cal, filename)
    return events
