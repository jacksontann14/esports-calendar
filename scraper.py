"""
Esports schedule toolkit — LoL + VALORANT.

LoL:      Riot/LoL Esports persisted API.
VALORANT: HenrikDev's VLR-backed v2 esports API.

Public API (kept compatible with generate.py):
    list_regions, list_teams, get_events, print_schedule,
    build_calendar, save_calendar, refresh_calendar
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from icalendar import Alarm, Calendar, Event

# ============================================================
# CONFIG
# ============================================================

LOL_API_KEY = os.environ.get("LOL_API_KEY", "YOUR_LOL_API_KEY")
LOL_BASE_URL = "https://esports-api.lolesports.com/persisted/gw"
LOL_HEADERS = {"x-api-key": LOL_API_KEY}
_LOL_ERROR_HINTS = {
    403: (
        "LoL API returned 403 Forbidden. This usually isn't a key problem — "
        "it's a strong signal that the request's source IP is being blocked "
        "by bot/WAF protection, which is common from GitHub Actions/cloud "
        "runner IPs even when the same request works fine locally."
    ),
}

# HenrikDev API key: https://api.henrikdev.xyz/dashboard
VALORANT_API_KEY = os.environ.get("VALORANT_API_KEY", "YOUR_HENRIKDEV_API_KEY")

# VLR-backed v2 API. Do NOT use the retired /valorant/v1/esports/schedule endpoint.
VALORANT_BASE_URL = "https://api.henrikdev.xyz/valorant/v2/esports/vlr"
VALORANT_HEADERS = {"Authorization": VALORANT_API_KEY}
_VALORANT_ERROR_HINTS = {
    401: (
        "VALORANT API returned 401 Unauthorized — check that VALORANT_API_KEY "
        "is a real key from https://api.henrikdev.xyz/dashboard/."
    ),
}

SUPPORTED_GAMES = {"lol", "valorant"}

# In-memory cache so a single run doesn't refetch the same VLR event/match data.
_valorant_cache: dict[str, dict[str, Any]] = {"events": {}, "matches": {}}

__all__ = [
    "list_regions",
    "list_teams",
    "get_events",
    "print_schedule",
    "build_calendar",
    "save_calendar",
    "refresh_calendar",
]


# ============================================================
# HELPERS
# ============================================================

def _parse_iso(timestamp: str) -> datetime:
    """Parse an ISO 8601 timestamp, whether it ends in 'Z' or an explicit offset."""
    dt = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(text: str | None) -> str:
    """Lowercase and strip whitespace/underscores/hyphens, for loose string matching."""
    return re.sub(r"[\s_-]+", "", (text or "").lower())


def _estimate_duration(format_str: str | None) -> timedelta:
    """Rough match duration from a best-of count (BO1=1h, BO3=2h, BO5=3h, else 1.5h)."""
    match = re.search(r"(\d+)", format_str or "")
    count = int(match.group(1)) if match else 3
    return {1: timedelta(hours=1), 3: timedelta(hours=2), 5: timedelta(hours=3)}.get(
        count, timedelta(hours=1, minutes=30)
    )


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    retries: int = 2,
    timeout: int = 30,
    error_hints: dict[int, str] | None = None,
) -> dict[str, Any]:
    """
    GET JSON with retries on transient 429/5xx responses, and a clearer
    RuntimeError message for status codes listed in `error_hints`.
    """
    error_hints = error_hints or {}

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt == retries:
                raise
            wait = 2**attempt
            print(f"Warning: request failed for {url} — retrying in {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code in error_hints:
            raise RuntimeError(error_hints[resp.status_code])

        if resp.status_code in {429, 500, 502, 503, 504} and attempt < retries:
            wait = 2**attempt
            print(f"Warning: {resp.status_code} from {url} — retrying in {wait}s...")
            time.sleep(wait)
            continue

        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected response from {url}: expected a JSON object.")
        return payload

    raise RuntimeError(f"Unable to fetch {url}")  # unreachable


# ============================================================
# LOL RAW FETCHERS
# ============================================================

def _lol_get_leagues() -> list[dict[str, Any]]:
    payload = _request_json(
        f"{LOL_BASE_URL}/getLeagues",
        headers=LOL_HEADERS,
        params={"hl": "en-US"},
        error_hints=_LOL_ERROR_HINTS,
    )
    return payload["data"]["leagues"]


def _lol_get_teams() -> list[dict[str, Any]]:
    payload = _request_json(
        f"{LOL_BASE_URL}/getTeams",
        headers=LOL_HEADERS,
        params={"hl": "en-US"},
        error_hints=_LOL_ERROR_HINTS,
    )
    return payload["data"]["teams"]


def _lol_find_league_ids(region_names: str | list[str]) -> list[str]:
    region_names = [region_names] if isinstance(region_names, str) else region_names
    leagues = _lol_get_leagues()

    matched = []
    for region in region_names:
        query = region.lower()
        hit = next(
            (lg for lg in leagues if query in lg["name"].lower() or query == lg["slug"].lower()),
            None,
        )
        if not hit:
            raise ValueError(f"[lol] No league found matching '{region}'")
        matched.append(hit["id"])
    return matched


def _lol_raw_schedule_single(league_id: str | None = None) -> list[dict[str, Any]]:
    """
    Fetch one league's paginated schedule (or the default unfiltered one if
    `league_id` is None). Kept separate from `_lol_raw_schedule` so
    multi-league calls can fetch one league at a time — see that
    function's docstring for why.
    """
    events: list[dict[str, Any]] = []
    params: dict[str, Any] = {"hl": "en-US"}
    if league_id:
        params["leagueId"] = [league_id]

    page_token: str | None = None
    while True:
        if page_token:
            params["pageToken"] = page_token

        payload = _request_json(
            f"{LOL_BASE_URL}/getSchedule",
            headers=LOL_HEADERS,
            params=params,
            error_hints=_LOL_ERROR_HINTS,
        )
        schedule = payload["data"]["schedule"]
        events.extend(schedule.get("events", []))

        older_token = schedule.get("pages", {}).get("older")
        if not older_token or older_token == page_token:
            break
        page_token = older_token

    return events


def _lol_raw_schedule(league_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Fetch the schedule across one or more leagues.

    IMPORTANT: sending multiple leagueId values in a single request only
    honors the LAST one (observed behavior), so each league is fetched in
    its own request and the results are merged and deduplicated by match id.
    """
    if not league_ids:
        return _lol_raw_schedule_single(None)

    all_events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for league_id in league_ids:
        for event in _lol_raw_schedule_single(league_id):
            match_id = event.get("match", {}).get("id")
            if match_id and match_id in seen_ids:
                continue
            if match_id:
                seen_ids.add(match_id)
            all_events.append(event)

    return all_events


# ============================================================
# VALORANT V2 (VLR) RAW FETCHERS
# ============================================================

def _valorant_get_events_page(
    page: int = 1,
    event_type: str = "upcoming",
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """
    One page of VLR events — GET /v2/esports/vlr/events?type=&page=.

    Pages are 1-indexed: `page=0` currently 500s on HenrikDev's end
    (confirmed against the live API), so pagination must start at 1.
    """
    cache_key = f"{event_type}:{page}"
    if not force_refresh and cache_key in _valorant_cache["events"]:
        return _valorant_cache["events"][cache_key]

    payload = _request_json(
        f"{VALORANT_BASE_URL}/events",
        headers=VALORANT_HEADERS,
        params={"type": event_type, "page": page},
        error_hints=_VALORANT_ERROR_HINTS,
    )
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("Unexpected VLR events response: 'data' is not a list.")

    _valorant_cache["events"][cache_key] = data
    return data


def _valorant_get_all_events(
    event_type: str = "upcoming",
    force_refresh: bool = False,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Page through VLR events (1-indexed) until an empty page comes back."""
    events: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        page_events = _valorant_get_events_page(page, event_type, force_refresh)
        if not page_events:
            break
        events.extend(page_events)
    return events


def _valorant_get_event_matches(
    event_id: int | str,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Matches for one VLR event — GET /v2/esports/vlr/events/{id}/matches."""
    event_id = str(event_id)
    if not force_refresh and event_id in _valorant_cache["matches"]:
        return _valorant_cache["matches"][event_id]

    payload = _request_json(
        f"{VALORANT_BASE_URL}/events/{event_id}/matches",
        headers=VALORANT_HEADERS,
        error_hints=_VALORANT_ERROR_HINTS,
    )
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected VLR matches response for event {event_id}: 'data' is not a list.")

    _valorant_cache["matches"][event_id] = data
    return data


def _valorant_event_matches_query(event: dict[str, Any], query: str) -> bool:
    """Loosely match a configured region/competition string against a VLR event's title/slug/region."""
    q = _norm(query)
    return any(q in _norm(event.get(field, "")) for field in ("title", "slug", "region"))


def _valorant_match_has_team(raw_match: dict[str, Any], team: str) -> bool:
    """Whether a VLR match (nested inside an event) includes the given team."""
    q = _norm(team)
    return any(
        q in _norm(t.get("name", "")) or q in _norm(t.get("tag", ""))
        for t in raw_match.get("teams", [])
        if isinstance(t, dict)
    )


def _valorant_normalize_match(event: dict[str, Any], raw_match: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one VLR match (nested inside an event) into the common event schema."""
    date_value = raw_match.get("date")
    if not date_value:
        return None
    try:
        start = _parse_iso(str(date_value))
    except (TypeError, ValueError):
        print(f"Warning: unable to parse VALORANT match date '{date_value}' — skipping.")
        return None

    teams = [
        str(t.get("name") or t.get("tag") or "TBD") if isinstance(t, dict) else str(t)
        for t in raw_match.get("teams", [])
    ] or ["TBD"]

    tags = " ".join(str(tag) for tag in raw_match.get("tags", []) or [] if tag)
    format_text = " ".join(part for part in (raw_match.get("series", ""), tags) if part).strip() or "VALORANT"

    # The events/matches endpoint doesn't expose a live/completed flag, so a
    # match counts as "completed" only once its estimated duration has
    # elapsed — otherwise a live match would vanish the instant it started.
    now = datetime.now(timezone.utc)
    state = "completed" if now >= start + _estimate_duration(format_text) else "unstarted"

    match_id = raw_match.get("id") or raw_match.get("slug") or uuid.uuid4()

    return {
        "game": "valorant",
        "id": str(match_id),
        "start": start,
        "league": str(event.get("title") or raw_match.get("event") or "VALORANT"),
        "identifier": str(event.get("slug", "")),
        "region": str(event.get("region", "")),
        "teams": teams,
        "format": format_text,
        "state": state,
    }


def _valorant_raw_schedule(
    regions: str | list[str] | None = None,
    team: str | None = None,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch VALORANT matches via HenrikDev's VLR-backed v2 API: list upcoming
    events, narrow to the ones matching `regions` (e.g. "VCT Pacific",
    "Masters", "Champions", "EWC"), then fetch and normalize each matching
    event's matches, optionally filtered further by `team`.
    """
    queries = [regions] if isinstance(regions, str) else list(regions) if regions else []
    events = _valorant_get_all_events(event_type="upcoming", force_refresh=force_refresh)

    if queries:
        matched_events = [e for e in events if any(_valorant_event_matches_query(e, q) for q in queries)]
        if not matched_events:
            print(f"Warning: no VALORANT VLR events matched configured regions: {queries}")
            if not team:
                return []
            # A team filter can still work by searching every event, even if
            # the configured region string didn't match any event's name.
        else:
            events = matched_events

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in events:
        event_id = event.get("id")
        if event_id is None:
            continue

        try:
            matches = _valorant_get_event_matches(event_id, force_refresh=force_refresh)
        except requests.RequestException as exc:
            print(f"Warning: unable to fetch VALORANT event {event_id} ({event.get('title', 'unknown')}): {exc}")
            continue

        for raw_match in matches:
            if team and not _valorant_match_has_team(raw_match, team):
                continue
            normalized_event = _valorant_normalize_match(event, raw_match)
            if not normalized_event or normalized_event["id"] in seen_ids:
                continue
            seen_ids.add(normalized_event["id"])
            normalized.append(normalized_event)

    return normalized


# ============================================================
# NORMALIZERS — LOL
# ============================================================

def _normalize_lol_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    match = raw.get("match")
    start_time = raw.get("startTime")
    if not match or not start_time:
        return None

    league = raw.get("league", {})
    return {
        "game": "lol",
        "id": match.get("id", str(uuid.uuid4())),
        "start": _parse_iso(start_time),
        "league": league.get("name", ""),
        "identifier": league.get("slug", ""),
        "region": league.get("name", ""),
        "teams": [team.get("name", "TBD") for team in match.get("teams", [])] or ["TBD"],
        "format": match.get("strategy", {}).get("type", ""),
        "state": raw.get("state", ""),
    }


# ============================================================
# PUBLIC API — DISCOVERY
# ============================================================

def list_regions(game: str, force_refresh: bool = False):
    """
    List available leagues/events.

    LoL:      authoritative LoL leagues.
    VALORANT: upcoming VLR events currently exposed by HenrikDev v2.
    """
    game = game.lower()

    if game == "lol":
        leagues = _lol_get_leagues()
        for league in sorted(leagues, key=lambda x: x.get("name", "")):
            print(f"{league.get('name', ''):<35} slug={league.get('slug', ''):<25} id={league.get('id', '')}")
        return leagues

    if game == "valorant":
        events = _valorant_get_all_events(event_type="upcoming", force_refresh=force_refresh)
        print("VALORANT upcoming VLR events:")
        for event in sorted(events, key=lambda x: x.get("dates", {}).get("start") or ""):
            dates = event.get("dates", {})
            print(
                f"{event.get('title', ''):<50} slug={event.get('slug', ''):<35} "
                f"region={event.get('region', ''):<20} start={dates.get('start') or ''}"
            )
        return events

    raise ValueError(f"Unsupported game '{game}'. Choose from {SUPPORTED_GAMES}")


def list_teams(game: str, region: str | None = None, force_refresh: bool = False):
    """
    List teams. LoL uses the real team directory; VALORANT is derived from
    teams visible in the current upcoming VLR event data.
    """
    game = game.lower()

    if game == "lol":
        teams = _lol_get_teams()
        if region:
            teams = [t for t in teams if region.lower() in t.get("homeLeague", {}).get("name", "").lower()]
        for t in sorted(teams, key=lambda x: x.get("code") or ""):
            home_league = t.get("homeLeague", {}).get("name", "N/A")
            print(f"{t.get('code', '???'):<8} {t.get('name', ''):<30} region={home_league}")
        return teams

    if game == "valorant":
        events = _valorant_get_all_events(event_type="upcoming", force_refresh=force_refresh)
        if region:
            events = [e for e in events if _valorant_event_matches_query(e, region)]

        seen: dict[str, str] = {}
        for event in events:
            event_id = event.get("id")
            if event_id is None:
                continue
            try:
                matches = _valorant_get_event_matches(event_id, force_refresh=force_refresh)
            except requests.RequestException:
                continue
            for match in matches:
                for raw_team in match.get("teams", []):
                    if not isinstance(raw_team, dict):
                        continue
                    name = raw_team.get("name") or raw_team.get("tag")
                    if name:
                        seen[str(name)] = str(event.get("region", ""))

        for name, reg in sorted(seen.items()):
            print(f"{name:<30} region={reg}")
        return seen

    raise ValueError(f"Unsupported game '{game}'. Choose from {SUPPORTED_GAMES}")


# ============================================================
# PUBLIC API — EVENT FETCHING
# ============================================================

def get_events(
    game: str,
    regions: str | list[str] | None = None,
    team: str | None = None,
    days_ahead: int | None = None,
    date_start: datetime | None = None,
    date_end: datetime | None = None,
    states: str | list[str] | None = None,
    exclude_tbd: bool = False,
    force_refresh: bool = False,
):
    """
    Fetch and filter match events.

    game:          "lol" or "valorant"
    regions:       LoL league names/slugs, or VALORANT competition/event
                   strings matched against VLR event title, slug and region
                   (e.g. "VCT Pacific", "vct_pacific", "Masters", "EWC").
    team:          substring match against participating team names/tags.
    days_ahead:    shortcut for "now -> now + N days".
    date_start/date_end: explicit UTC-aware datetime range (used instead of days_ahead).
    states:        "unstarted" / "completed" ("inProgress" is accepted too, but
                    VALORANT v2 has no live-state field so it folds into "unstarted").
    exclude_tbd:   drop matches where a team slot is still "TBD".
    force_refresh: bypass the VALORANT in-memory cache.

    Returns a deduplicated list of normalized event dicts, sorted by start time.
    """
    game = game.lower()

    if game == "lol":
        league_ids = _lol_find_league_ids(regions) if regions else None
        raw_events = _lol_raw_schedule(league_ids)
        events = [e for raw in raw_events if (e := _normalize_lol_event(raw))]

    elif game == "valorant":
        events = _valorant_raw_schedule(regions=regions, team=team, force_refresh=force_refresh)

    else:
        raise ValueError(f"Unsupported game '{game}'. Choose from {SUPPORTED_GAMES}")

    if team:
        query = team.lower()
        events = [e for e in events if any(query in name.lower() for name in e["teams"])]

    if days_ahead is not None:
        date_start = datetime.now(timezone.utc)
        date_end = date_start + timedelta(days=days_ahead)
    date_start, date_end = _to_utc(date_start), _to_utc(date_end)
    if date_start:
        events = [e for e in events if e["start"] >= date_start]
    if date_end:
        events = [e for e in events if e["start"] <= date_end]

    if states:
        wanted = {s.lower() for s in ([states] if isinstance(states, str) else states)}
        events = [e for e in events if e["state"].lower() in wanted]

    if exclude_tbd:
        events = [e for e in events if "TBD" not in e["teams"]]

    deduped = {str(e["id"]): e for e in events}
    return sorted(deduped.values(), key=lambda e: e["start"])


# ============================================================
# CALENDAR OUTPUT
# ============================================================

def print_schedule(events: list[dict[str, Any]], local_tz: str = "America/New_York") -> None:
    """Console preview of events in a local timezone."""
    tz = ZoneInfo(local_tz)
    for event in events:
        local_time = event["start"].astimezone(tz)
        matchup = " vs ".join(event["teams"])
        print(
            f"{local_time.strftime('%Y-%m-%d %H:%M %Z'):<25} "
            f"[{event['game'].upper()}][{event['league']}] {matchup}"
        )


def build_calendar(
    events: list[dict[str, Any]],
    cal_name: str = "Esports Schedule",
    text_header: str | None = None,
    include_league_tag: bool = True,
    add_reminder_minutes: int | None = None,
) -> Calendar:
    """Build an iCalendar Calendar from normalized events."""
    cal = Calendar()
    cal.add("prodid", "-//esports-scraper//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cal_name)

    for event in events:
        matchup = " vs ".join(event["teams"])
        base_summary = f"[{event['game'].upper()}][{event['league']}] {matchup}" if include_league_tag else matchup
        summary = f"{text_header}: {base_summary}" if text_header else base_summary

        vevent = Event()
        vevent.add("uid", f"{event['id']}@{event['game']}-scraper")
        vevent.add("summary", summary)
        vevent.add("dtstart", event["start"])
        vevent.add("dtend", event["start"] + _estimate_duration(event["format"]))
        vevent.add("description", f"Format: {event['format']}\nState: {event['state']}\nRegion: {event['region']}")

        if add_reminder_minutes:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", f"Reminder: {summary}")
            alarm.add("trigger", timedelta(minutes=-add_reminder_minutes))
            vevent.add_component(alarm)

        cal.add_component(vevent)

    return cal


def save_calendar(cal: Calendar, filename: str) -> None:
    """Write an iCalendar object to disk."""
    with open(filename, "wb") as f:
        f.write(cal.to_ical())
    print(f"Saved {len(cal.subcomponents)} events to {filename}")


def refresh_calendar(
    game: str,
    output: str,
    *,
    regions: str | list[str] | None = None,
    team: str | None = None,
    days_ahead: int | None = None,
    date_start: datetime | None = None,
    date_end: datetime | None = None,
    states: str | list[str] | None = None,
    exclude_tbd: bool = False,
    cal_name: str = "Esports Schedule",
    text_header: str | None = None,
    include_league_tag: bool = True,
    add_reminder_minutes: int | None = None,
) -> Calendar:
    """Convenience wrapper for scripts that want to fetch and immediately save one calendar."""
    events = get_events(
        game,
        regions=regions,
        team=team,
        days_ahead=days_ahead,
        date_start=date_start,
        date_end=date_end,
        states=states,
        exclude_tbd=exclude_tbd,
        force_refresh=True,
    )
    calendar = build_calendar(
        events,
        cal_name=cal_name,
        text_header=text_header,
        include_league_tag=include_league_tag,
        add_reminder_minutes=add_reminder_minutes,
    )
    save_calendar(calendar, output)
    return calendar
