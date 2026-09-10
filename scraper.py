"""
Esports schedule toolkit — LoL + VALORANT

LoL:
    Riot/LoL Esports persisted API.

VALORANT:
    HenrikDev VLR-backed v2 esports API.

The public API is intentionally kept compatible with generate.py:
    list_regions()
    list_teams()
    get_events()
    print_schedule()
    build_calendar()
    save_calendar()
    refresh_calendar()
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

import requests
from icalendar import Alarm, Calendar, Event


# ============================================================
# CONFIG
# ============================================================

LOL_API_KEY: Final[str] = os.environ.get(
    "LOL_API_KEY",
    "YOUR_HENRIKDEV_API_KEY",
)

LOL_BASE_URL: Final[str] = (
    "https://esports-api.lolesports.com/persisted/gw"
)

LOL_HEADERS: Final[dict[str, str]] = {
    "x-api-key": LOL_API_KEY,
}


# HenrikDev API key:
# https://api.henrikdev.xyz/dashboard
VALORANT_API_KEY: Final[str] = os.environ.get(
    "VALORANT_API_KEY",
    "YOUR_HENRIKDEV_API_KEY",
)

# IMPORTANT:
# This is the newer VLR-backed v2 API.
# Do NOT use the old /valorant/v1/esports/schedule endpoint.
VALORANT_V2_BASE_URL: Final[str] = (
    "https://api.henrikdev.xyz/valorant/v2/esports/vlr"
)

VALORANT_HEADERS: dict[str, str] = {
    "Authorization": VALORANT_API_KEY,
}


SUPPORTED_GAMES: Final[set[str]] = {
    "lol",
    "valorant",
}


# Small in-memory cache so a single GitHub Actions run does not
# repeatedly request the same VLR event/match data.
_valorant_cache: dict[str, Any] = {
    "events": {},
    "matches": {},
}


# ============================================================
# PUBLIC EXPORTS
# ============================================================

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
# INTERNAL HELPERS
# ============================================================

def _parse_iso(timestamp: str) -> datetime:
    """Parse ISO 8601 timestamps with Z or explicit offsets."""

    if not timestamp:
        raise ValueError("Missing timestamp")

    timestamp = timestamp.strip()

    if timestamp.endswith("Z"):
        timestamp = timestamp[:-1] + "+00:00"

    dt = datetime.fromisoformat(timestamp)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _norm(text: str | None) -> str:
    """
    Lowercase and remove whitespace/underscores/hyphens.

    Used for loose matching of configured region/event/team names.
    """
    return re.sub(
        r"[\s_\-]+",
        "",
        (text or "").lower(),
    )


def _estimate_duration(format_str: str | None) -> timedelta:
    """
    Rough match duration based on the best-of number.

    BO1 -> 1 hour
    BO3 -> 2 hours
    BO5 -> 3 hours
    otherwise -> 1.5 hours
    """

    match = re.search(r"(\d+)", format_str or "")
    count = int(match.group(1)) if match else 3

    durations = {
        1: timedelta(hours=1),
        3: timedelta(hours=2),
        5: timedelta(hours=3),
    }

    return durations.get(
        count,
        timedelta(hours=1, minutes=30),
    )


def _to_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware."""

    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    retries: int = 2,
    timeout: int = 30,
) -> dict[str, Any]:
    """
    GET JSON with lightweight retry handling.

    HenrikDev can occasionally return transient 5xx responses.
    We retry those instead of immediately killing the calendar build.
    """

    last_response: requests.Response | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            )

            last_response = response

            # Retry transient server/rate-limit errors.
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt < retries:
                    wait_seconds = 2 ** attempt
                    print(
                        f"Warning: {response.status_code} from "
                        f"{url} — retrying in {wait_seconds}s..."
                    )
                    time.sleep(wait_seconds)
                    continue

            if response.status_code == 401:
                raise RuntimeError(
                    "API returned 401 Unauthorized. "
                    "Check that the API key is valid and that the "
                    "correct GitHub Actions secret is configured."
                )

            response.raise_for_status()

            payload = response.json()

            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unexpected API response from {url}: "
                    f"expected JSON object."
                )

            return payload

        except requests.RequestException:
            if attempt < retries:
                wait_seconds = 2 ** attempt
                print(
                    f"Warning: request failed for {url} "
                    f"— retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                continue

            raise

    if last_response is not None:
        last_response.raise_for_status()

    raise RuntimeError(f"Unable to fetch {url}")


# ============================================================
# LOL RAW FETCHERS
# ============================================================

def _lol_check_response(resp: requests.Response) -> None:
    """
    Raise a clearer error for LoL API responses.

    GitHub Actions/cloud runners can sometimes receive 403s from
    upstream WAF/bot protection.
    """

    if resp.status_code == 403:
        raise RuntimeError(
            "LoL API returned 403 Forbidden. "
            "This may indicate upstream WAF/bot protection, "
            "particularly from GitHub Actions/cloud runner IPs."
        )

    resp.raise_for_status()


def _lol_get_leagues() -> list[dict[str, Any]]:
    resp = requests.get(
        f"{LOL_BASE_URL}/getLeagues",
        headers=LOL_HEADERS,
        params={"hl": "en-US"},
        timeout=30,
    )

    _lol_check_response(resp)

    return resp.json()["data"]["leagues"]


def _lol_get_teams() -> list[dict[str, Any]]:
    resp = requests.get(
        f"{LOL_BASE_URL}/getTeams",
        headers=LOL_HEADERS,
        params={"hl": "en-US"},
        timeout=30,
    )

    _lol_check_response(resp)

    return resp.json()["data"]["teams"]


def _lol_find_league_ids(
    region_names: str | list[str],
) -> list[str]:
    region_names = (
        [region_names]
        if isinstance(region_names, str)
        else region_names
    )

    leagues = _lol_get_leagues()

    matched: list[str] = []

    for region in region_names:
        query = region.lower()

        hit = next(
            (
                league
                for league in leagues
                if (
                    query in league["name"].lower()
                    or query == league["slug"].lower()
                )
            ),
            None,
        )

        if not hit:
            raise ValueError(
                f"[lol] No league found matching '{region}'"
            )

        matched.append(hit["id"])

    return matched


def _lol_raw_schedule_single(
    league_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch one LoL league schedule.

    Kept separate because sending multiple leagueId parameters
    to the LoL API has historically behaved inconsistently.
    """

    events: list[dict[str, Any]] = []
    page_token: str | None = None

    params: dict[str, Any] = {
        "hl": "en-US",
    }

    if league_id:
        params["leagueId"] = [league_id]

    while True:

        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(
            f"{LOL_BASE_URL}/getSchedule",
            headers=LOL_HEADERS,
            params=params,
            timeout=30,
        )

        _lol_check_response(resp)

        schedule = resp.json()["data"]["schedule"]

        events.extend(
            schedule.get("events", [])
        )

        older_token = (
            schedule
            .get("pages", {})
            .get("older")
        )

        if not older_token or older_token == page_token:
            break

        page_token = older_token

    return events


def _lol_raw_schedule(
    league_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch LoL schedule across one or more leagues.
    """

    if not league_ids:
        return _lol_raw_schedule_single(None)

    all_events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for league_id in league_ids:

        for event in _lol_raw_schedule_single(league_id):

            match_id = (
                event
                .get("match", {})
                .get("id")
            )

            if match_id and match_id in seen_ids:
                continue

            if match_id:
                seen_ids.add(match_id)

            all_events.append(event)

    return all_events


# ============================================================
# VALORANT V2 RAW FETCHERS
# ============================================================

def _valorant_get_events_page(
    page: int = 0,
    event_type: str = "upcoming",
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch one page of VLR events.

    HenrikDev documents:
        GET /valorant/v2/esports/vlr/events

    Supported type values:
        upcoming
        completed
    """

    cache_key = f"{event_type}:{page}"

    if (
        not force_refresh
        and cache_key in _valorant_cache["events"]
    ):
        return _valorant_cache["events"][cache_key]

    payload = _request_json(
        f"{VALORANT_V2_BASE_URL}/events",
        headers=VALORANT_HEADERS,
        params={
            "type": event_type,
            "page": page,
        },
    )

    data = payload.get("data", [])

    if not isinstance(data, list):
        raise RuntimeError(
            "Unexpected HenrikDev V2 events response: "
            "'data' is not a list."
        )

    _valorant_cache["events"][cache_key] = data

    return data


def _valorant_get_all_events(
    event_type: str = "upcoming",
    force_refresh: bool = False,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """
    Fetch VLR event pages until an empty page is returned.

    A maximum page count prevents a broken pagination response from
    creating an infinite loop.
    """

    events: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for page in range(max_pages):

        page_events = _valorant_get_events_page(
            page=page,
            event_type=event_type,
            force_refresh=force_refresh,
        )

        if not page_events:
            break

        for event in page_events:

            event_id = str(event.get("id", ""))

            if event_id and event_id in seen_ids:
                continue

            if event_id:
                seen_ids.add(event_id)

            events.append(event)

    return events


def _valorant_get_event_matches(
    event_id: int | str,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch matches for a VLR event.

    HenrikDev documents:
        GET /valorant/v2/esports/vlr/events/{event_id}/matches
    """

    event_id = str(event_id)

    if (
        not force_refresh
        and event_id in _valorant_cache["matches"]
    ):
        return _valorant_cache["matches"][event_id]

    payload = _request_json(
        f"{VALORANT_V2_BASE_URL}/events/{event_id}/matches",
        headers=VALORANT_HEADERS,
    )

    data = payload.get("data", [])

    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected HenrikDev V2 matches response "
            f"for event {event_id}: 'data' is not a list."
        )

    _valorant_cache["matches"][event_id] = data

    return data


def _valorant_event_matches_region(
    event: dict[str, Any],
    query: str,
) -> bool:
    """
    Match a configured region/competition string against VLR
    event metadata.

    This deliberately checks:
        title
        slug
        region
    """

    q = _norm(query)

    fields = (
        event.get("title", ""),
        event.get("slug", ""),
        event.get("region", ""),
    )

    return any(
        q in _norm(field)
        for field in fields
    )


def _valorant_event_matches_team(
    match: dict[str, Any],
    team: str,
) -> bool:
    """
    Check whether a VLR match contains the requested team.
    """

    query = _norm(team)

    for raw_team in match.get("teams", []):

        if not isinstance(raw_team, dict):
            continue

        name = raw_team.get("name", "")
        tag = raw_team.get("tag", "")

        if (
            query in _norm(name)
            or query in _norm(tag)
        ):
            return True

    return False


def _valorant_normalize_match(
    event: dict[str, Any],
    raw_match: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Convert HenrikDev V2 VLR match schema into the common event schema
    used by the calendar builder.

    V2 match structure documented by HenrikDev:

        {
            "event": "...",
            "id": 1,
            "series": "...",
            "slug": "...",
            "tags": [...],
            "teams": [...],
            "date": "..."
        }
    """

    date_value = raw_match.get("date")

    if not date_value:
        return None

    try:
        start = _parse_iso(str(date_value))
    except (TypeError, ValueError):
        print(
            f"Warning: unable to parse VALORANT match date "
            f"'{date_value}' — skipping match."
        )
        return None

    raw_teams = raw_match.get("teams", [])

    teams: list[str] = []

    for raw_team in raw_teams:

        if isinstance(raw_team, dict):
            name = (
                raw_team.get("name")
                or raw_team.get("tag")
                or "TBD"
            )
            teams.append(str(name))

        elif raw_team:
            teams.append(str(raw_team))

    if not teams:
        teams = ["TBD"]

    series = str(
        raw_match.get("series")
        or ""
    ).strip()

    tags = raw_match.get("tags", [])

    if isinstance(tags, list):
        tag_text = " ".join(
            str(tag)
            for tag in tags
            if tag
        )
    else:
        tag_text = str(tags or "")

    format_text = " ".join(
        part
        for part in (series, tag_text)
        if part
    ).strip()

    if not format_text:
        format_text = "VALORANT"

    # V2 does not expose the same "state" field that v1 did.
    # Infer state from match time so existing calendars.yaml
    # states: ["inProgress", "unstarted"] continues to work.
    now = datetime.now(timezone.utc)

    if start > now:
        state = "unstarted"
    else:
        # We don't have exact match completion data in this endpoint.
        # Treat past matches as completed.
        state = "completed"

    event_title = str(
        event.get("title")
        or raw_match.get("event")
        or "VALORANT"
    )

    event_slug = str(
        event.get("slug")
        or ""
    )

    event_region = str(
        event.get("region")
        or ""
    )

    match_id = raw_match.get("id")

    if match_id is None:
        match_id = raw_match.get("slug")

    if match_id is None:
        match_id = str(uuid.uuid4())

    return {
        "game": "valorant",
        "id": str(match_id),
        "start": start,
        "league": event_title,
        "identifier": event_slug,
        "region": event_region,
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
    Fetch VALORANT matches using HenrikDev's VLR-backed V2 API.

    Unlike the old v1 schedule endpoint, this does NOT call:

        /valorant/v1/esports/schedule

    Instead it:

        1. Gets VLR events.
        2. Filters events by configured region/competition.
        3. Gets matches for those events.
        4. Optionally filters by team.

    This is especially useful for:
        - VCT Pacific
        - Masters
        - Champions
        - Esports World Cup
        - other VLR-tracked events
    """

    queries: list[str] = []

    if regions:
        queries = (
            [regions]
            if isinstance(regions, str)
            else list(regions)
        )

    # We need upcoming events for calendar generation.
    upcoming_events = _valorant_get_all_events(
        event_type="upcoming",
        force_refresh=force_refresh,
    )

    selected_events: list[dict[str, Any]] = []

    if queries:

        for event in upcoming_events:

            if any(
                _valorant_event_matches_region(event, query)
                for query in queries
            ):
                selected_events.append(event)

        # If a configured region doesn't match an event title/slug/region,
        # don't immediately fail the entire calendar. This is useful because
        # VLR event naming can differ between editions.
        #
        # The team filter below still provides the final safety net.
        if not selected_events:
            print(
                "Warning: no VALORANT VLR events matched configured "
                f"regions: {queries}"
            )

    else:
        selected_events = upcoming_events

    # If a team is supplied and no region matched, fetching all upcoming
    # events is more useful than returning an empty calendar. The final
    # team filter will narrow it down.
    if team and not selected_events:
        selected_events = upcoming_events

    # If there is no team and there are no region matches, return empty.
    if not selected_events:
        return []

    normalized: list[dict[str, Any]] = []
    seen_match_ids: set[str] = set()

    for event in selected_events:

        event_id = event.get("id")

        if event_id is None:
            continue

        try:
            matches = _valorant_get_event_matches(
                event_id,
                force_refresh=force_refresh,
            )
        except requests.RequestException as exc:
            print(
                f"Warning: unable to fetch VALORANT event "
                f"{event_id} ({event.get('title', 'unknown')}): {exc}"
            )
            continue

        for raw_match in matches:

            if team and not _valorant_event_matches_team(
                raw_match,
                team,
            ):
                continue

            normalized_event = _valorant_normalize_match(
                event,
                raw_match,
            )

            if not normalized_event:
                continue

            match_id = normalized_event["id"]

            if match_id in seen_match_ids:
                continue

            seen_match_ids.add(match_id)
            normalized.append(normalized_event)

    return normalized


# ============================================================
# NORMALIZERS — LOL
# ============================================================

def _normalize_lol_event(
    raw: dict[str, Any],
) -> dict[str, Any] | None:

    match = raw.get("match")

    if not match:
        return None

    league = raw.get("league", {})

    start_time = raw.get("startTime")

    if not start_time:
        return None

    return {
        "game": "lol",
        "id": match.get(
            "id",
            str(uuid.uuid4()),
        ),
        "start": _parse_iso(start_time),
        "league": league.get("name", ""),
        "identifier": league.get("slug", ""),
        "region": league.get("name", ""),
        "teams": [
            team.get("name", "TBD")
            for team in match.get("teams", [])
        ] or ["TBD"],
        "format": match.get(
            "strategy",
            {},
        ).get("type", ""),
        "state": raw.get("state", ""),
    }


# ============================================================
# REGION MATCHING
# ============================================================

def _matches_region_query(
    event: dict[str, Any],
    query: str,
) -> bool:

    q = _norm(query)

    return any(
        q in _norm(field)
        for field in (
            event.get("league", ""),
            event.get("identifier", ""),
            event.get("region", ""),
        )
    )


# ============================================================
# PUBLIC API — DISCOVERY
# ============================================================

def list_regions(
    game: str,
    force_refresh: bool = False,
):
    """
    List available leagues/events.

    LoL:
        Returns authoritative LoL leagues.

    VALORANT:
        Returns upcoming VLR events currently exposed by HenrikDev V2.
    """

    game = game.lower()

    if game == "lol":

        leagues = _lol_get_leagues()

        for league in sorted(
            leagues,
            key=lambda x: x.get("name", ""),
        ):
            print(
                f"{league.get('name', ''):<35} "
                f"slug={league.get('slug', ''):<25} "
                f"id={league.get('id', '')}"
            )

        return leagues

    if game == "valorant":

        events = _valorant_get_all_events(
            event_type="upcoming",
            force_refresh=force_refresh,
        )

        print(
            "VALORANT upcoming VLR events:"
        )

        for event in sorted(
            events,
            key=lambda x: x.get("dates", {}).get("start", ""),
        ):
            dates = event.get("dates", {})

            print(
                f"{event.get('title', ''):<50} "
                f"slug={event.get('slug', ''):<35} "
                f"region={event.get('region', ''):<20} "
                f"start={dates.get('start', '')}"
            )

        return events

    raise ValueError(
        f"Unsupported game '{game}'. "
        f"Choose from {SUPPORTED_GAMES}"
    )


def list_teams(
    game: str,
    region: str | None = None,
    force_refresh: bool = False,
):
    """
    List teams visible in the current upcoming VALORANT event data.

    LoL uses the real LoL team directory.
    """

    game = game.lower()

    if game == "lol":

        teams = _lol_get_teams()

        if region:
            teams = [
                team
                for team in teams
                if region.lower()
                in team.get(
                    "homeLeague",
                    {},
                ).get(
                    "name",
                    "",
                ).lower()
            ]

        for team in sorted(
            teams,
            key=lambda x: x.get("code") or "",
        ):
            print(
                f"{team.get('code', '???'):<8} "
                f"{team.get('name', ''):<30} "
                f"region="
                f"{team.get('homeLeague', {}).get('name', 'N/A')}"
            )

        return teams

    if game == "valorant":

        events = _valorant_get_all_events(
            event_type="upcoming",
            force_refresh=force_refresh,
        )

        if region:
            events = [
                event
                for event in events
                if _valorant_event_matches_region(
                    event,
                    region,
                )
            ]

        seen: dict[str, str] = {}

        for event in events:

            event_id = event.get("id")

            if event_id is None:
                continue

            try:
                matches = _valorant_get_event_matches(
                    event_id,
                    force_refresh=force_refresh,
                )
            except requests.RequestException:
                continue

            for match in matches:

                for raw_team in match.get("teams", []):

                    if not isinstance(raw_team, dict):
                        continue

                    name = (
                        raw_team.get("name")
                        or raw_team.get("tag")
                    )

                    if name:
                        seen[str(name)] = str(
                            event.get("region", "")
                        )

        for name, reg in sorted(seen.items()):
            print(
                f"{name:<30} region={reg}"
            )

        return seen

    raise ValueError(
        f"Unsupported game '{game}'. "
        f"Choose from {SUPPORTED_GAMES}"
    )


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

    Parameters
    ----------
    game:
        'lol' or 'valorant'

    regions:
        LoL league names/slugs.

        VALORANT competition/event strings. These are matched against
        VLR event title, slug and region.

        Examples:
            "VCT Pacific"
            "vct_pacific"
            "Masters"
            "Champions"
            "EWC"
            "Esports World Cup"

    team:
        Substring match against participating team names/tags.

    days_ahead:
        Shortcut for:
            now -> now + N days

    date_start / date_end:
        Explicit UTC-aware datetime range.

    states:
        Existing values remain supported:
            "unstarted"
            "completed"

        "inProgress" is retained for compatibility, although the VLR
        event-match endpoint does not expose an explicit live-state field.

    exclude_tbd:
        Drop matches containing TBD.

    Returns
    -------
    list[dict]
        Normalized, deduplicated, sorted events.
    """

    game = game.lower()

    # --------------------------------------------------------
    # LoL
    # --------------------------------------------------------

    if game == "lol":

        league_ids = (
            _lol_find_league_ids(regions)
            if regions
            else None
        )

        raw_events = _lol_raw_schedule(
            league_ids
        )

        events = [
            normalized
            for raw in raw_events
            if (
                normalized := _normalize_lol_event(raw)
            )
        ]

    # --------------------------------------------------------
    # VALORANT
    # --------------------------------------------------------

    elif game == "valorant":

        events = _valorant_raw_schedule(
            regions=regions,
            team=team,
            force_refresh=force_refresh,
        )

    else:

        raise ValueError(
            f"Unsupported game '{game}'. "
            f"Choose from {SUPPORTED_GAMES}"
        )

    # --------------------------------------------------------
    # Team filter
    # --------------------------------------------------------

    if team:

        query = team.lower()

        events = [
            event
            for event in events
            if any(
                query in team_name.lower()
                for team_name in event["teams"]
            )
        ]

    # --------------------------------------------------------
    # Date range
    # --------------------------------------------------------

    if days_ahead is not None:

        date_start = datetime.now(
            timezone.utc
        )

        date_end = (
            date_start
            + timedelta(days=days_ahead)
        )

    date_start = _to_utc(date_start)
    date_end = _to_utc(date_end)

    if date_start:

        events = [
            event
            for event in events
            if event["start"] >= date_start
        ]

    if date_end:

        events = [
            event
            for event in events
            if event["start"] <= date_end
        ]

    # --------------------------------------------------------
    # State filter
    # --------------------------------------------------------

    if states:

        wanted = {
            state.lower()
            for state in (
                [states]
                if isinstance(states, str)
                else states
            )
        }

        events = [
            event
            for event in events
            if event["state"].lower()
            in wanted
        ]

    # --------------------------------------------------------
    # TBD filter
    # --------------------------------------------------------

    if exclude_tbd:

        events = [
            event
            for event in events
            if "TBD" not in event["teams"]
        ]

    # --------------------------------------------------------
    # Dedupe
    # --------------------------------------------------------

    deduped: dict[str, dict[str, Any]] = {}

    for event in events:
        deduped[str(event["id"])] = event

    events = list(deduped.values())

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    return sorted(
        events,
        key=lambda event: event["start"],
    )


# ============================================================
# CALENDAR OUTPUT
# ============================================================

def print_schedule(
    events: list[dict[str, Any]],
    local_tz: str = "America/New_York",
) -> None:
    """
    Console preview of events in a local timezone.
    """

    tz = ZoneInfo(local_tz)

    for event in events:

        local_time = event["start"].astimezone(tz)

        matchup = " vs ".join(
            event["teams"]
        )

        print(
            f"{local_time.strftime('%Y-%m-%d %H:%M %Z'):<25} "
            f"[{event['game'].upper()}]"
            f"[{event['league']}] "
            f"{matchup}"
        )


def build_calendar(
    events: list[dict[str, Any]],
    cal_name: str = "Esports Schedule",
    text_header: str | None = None,
    include_league_tag: bool = True,
    add_reminder_minutes: int | None = None,
) -> Calendar:
    """
    Build an iCalendar Calendar from normalized events.
    """

    cal = Calendar()

    cal.add(
        "prodid",
        "-//esports-scraper//EN",
    )

    cal.add(
        "version",
        "2.0",
    )

    cal.add(
        "x-wr-calname",
        cal_name,
    )

    for event in events:

        matchup = " vs ".join(
            event["teams"]
        )

        if include_league_tag:

            base_summary = (
                f"[{event['game'].upper()}]"
                f"[{event['league']}] "
                f"{matchup}"
            )

        else:

            base_summary = matchup

        if text_header:

            summary = (
                f"{text_header}: "
                f"{base_summary}"
            )

        else:

            summary = base_summary

        vevent = Event()

        vevent.add(
            "uid",
            f"{event['id']}"
            f"@{event['game']}"
            f"-scraper",
        )

        vevent.add(
            "summary",
            summary,
        )

        vevent.add(
            "dtstart",
            event["start"],
        )

        vevent.add(
            "dtend",
            event["start"]
            + _estimate_duration(
                event["format"]
            ),
        )

        description = (
            f"Format: {event['format']}\n"
            f"State: {event['state']}\n"
            f"Region: {event['region']}"
        )

        vevent.add(
            "description",
            description,
        )

        if add_reminder_minutes:

            alarm = Alarm()

            alarm.add(
                "action",
                "DISPLAY",
            )

            alarm.add(
                "description",
                f"Reminder: {summary}",
            )

            alarm.add(
                "trigger",
                timedelta(
                    minutes=-add_reminder_minutes
                ),
            )

            vevent.add_component(alarm)

        cal.add_component(vevent)

    return cal


def save_calendar(
    cal: Calendar,
    filename: str,
) -> None:
    """
    Write an iCalendar object to disk.
    """

    with open(
        filename,
        "wb",
    ) as file:

        file.write(
            cal.to_ical()
        )

    print(
        f"Saved "
        f"{len(cal.subcomponents)} events "
        f"to {filename}"
    )


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
    """
    Convenience wrapper used by scripts that want to fetch and
    immediately save one calendar.
    """

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

    save_calendar(
        calendar,
        output,
    )

    return calendar