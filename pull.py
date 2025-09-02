#!/usr/bin/env python3
"""
pull.py — minimal ESPN Fantasy Football league snapshot

What this script collects (and nothing more):
1) Final results from previous years (rank, W/L/T, PF, PA)
2) Current team records
3) Points For (PF) and Points Against (PA)
4) League size & teams

Output: out/llm_basic.json

Dependencies:
  - espn-api  (pip install espn-api)
  - requests  (comes with espn-api dep, but we import directly)

Auth:
  - Public leagues: no cookies required
  - Private leagues: set ESPN_S2 and SWID env vars

Required env:
  - LEAGUE_ID  (int)
Optional env:
  - YEAR              (defaults to current NFL season you’re pulling; e.g., 2025)
  - HISTORY_YEARS     (defaults to 3; how many prior seasons to fetch)
  - ESPN_S2, SWID     (only for private leagues)

Extend later:
  - Add schedule, rosters, projections, etc., by introducing more views or using league methods.
"""

import os
import sys
import json
import time
import random
import datetime as dt
import requests
from espn_api.football import League

# ================================
# ---- Environment & guards ----
# ================================
LEAGUE_ID = os.getenv("LEAGUE_ID")
YEAR = int(os.getenv("YEAR", "2025"))
HISTORY_YEARS = int(os.getenv("HISTORY_YEARS", "3"))

# Optional cookies for private leagues
ESPN_S2 = os.getenv("ESPN_S2") or None
SWID = os.getenv("SWID") or None

if not LEAGUE_ID or not LEAGUE_ID.strip():
    sys.exit("ERROR: LEAGUE_ID is empty. Provide it via env (GitHub secret or step env).")

try:
    LEAGUE_ID = int(LEAGUE_ID)
except ValueError:
    sys.exit("ERROR: LEAGUE_ID must be an integer.")

# ================================
# ---- League init (espn_api) ----
# ================================
try:
    league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
except Exception as e:
    sys.exit(f"ERROR: Could not initialize League. Check LEAGUE_ID/YEAR and cookies. Details: {e}")

# ================================
# ---- Minimal helpers (HTTP) ----
# ================================
# We only need leagueHistory for prior seasons (rank/WLT/PF/PA).
# ESPN’s v3 leagueHistory endpoint:
#   https://fantasy.espn.com/apis/v3/games/ffl/leagueHistory/{leagueId}?seasonId=YYYY&view=mStandings
BASE_HISTORY = f"https://fantasy.espn.com/apis/v3/games/ffl/leagueHistory/{LEAGUE_ID}"

COOKIES = {}
if ESPN_S2: COOKIES["espn_s2"] = ESPN_S2
if SWID:    COOKIES["SWID"] = SWID

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (llm-basic/1.0)",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://fantasy.espn.com/football/league?leagueId={LEAGUE_ID}",
    "x-fantasy-source": "kona-PWA",
    "x-fantasy-platform": "kona-PWA",
}

def GET(url, params=None, timeout=20, retries=3, backoff=0.6):
    """Basic GET with ESPN-friendly headers and tiny retry."""
    if params is None:
        params = {}
    # cache-buster param
    params["_"] = str(random.randint(10**6, 10**7 - 1))
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=COMMON_HEADERS, cookies=COOKIES, timeout=timeout)
            if r.ok:
                return r.json()
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(backoff * (attempt + 1))
    # Return empty object on failure; caller can skip
    if last_err:
        print(f"WARN: GET {url} failed after retries: {last_err}")
    return {}

# New function to get manager names from previous years
# ---------- Helpers to resolve past-season managers ----------

def _format_member_name(m):
    """Prefer 'First Last'; otherwise fall back gracefully."""
    first = (m.get("firstName") or "").strip()
    last  = (m.get("lastName")  or "").strip()
    if first or last:
        return f"{first} {last}".strip()
    return (m.get("displayName") or m.get("nickname") or m.get("email") or m.get("id") or "").strip()

def _members_name_map_for_season(league_id, season):
    """
    Returns {ownerId: 'First Last'} for the given season using view=mMembers.
    Falls back to CURRENT season if the past season's mMembers is empty.
    """
    base = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
    data = GET(base, params={"view": "mMembers"})
    members = data.get("members") or []

    # Fallback: older seasons sometimes don't return mMembers. Use current season as a best-effort.
    if not members and season != YEAR:
        base_now = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{league_id}"
        data_now = GET(base_now, params={"view": "mMembers"})
        members = data_now.get("members") or []

    return {m.get("id"): _format_member_name(m) for m in members if m.get("id")}

def _owners_by_team_from_history(league_id, season):
    """
    Build {teamId: [ownerIds...]} for a past season.
    Tries leagueHistory (mStandings) first; then season's mTeam as fallback.
    """
    owners_map = {}

    # A) leagueHistory (often has owners/primaryOwner)
    payload = GET(
        f"https://fantasy.espn.com/apis/v3/games/ffl/leagueHistory/{league_id}",
        params={"seasonId": season, "view": "mStandings"}
    )
    data = payload[0] if isinstance(payload, list) and payload else (payload if isinstance(payload, dict) else None)
    if isinstance(data, dict):
        for t in (data.get("teams") or []):
            tid = t.get("id")
            ids = list(t.get("owners") or [])
            primary = t.get("primaryOwner")
            if primary and primary not in ids:
                ids.append(primary)
            if tid is not None:
                owners_map[tid] = ids

    # B) fallback: season mTeam
    if not owners_map:
        base = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
        raw_team = GET(base, params={"view": "mTeam"})
        for t in (raw_team.get("teams") or []):
            tid = t.get("id")
            ids = list(t.get("owners") or [])
            primary = t.get("primaryOwner")
            if primary and primary not in ids:
                ids.append(primary)
            if tid is not None:
                owners_map[tid] = ids

    return owners_map

def _manager_names_for_season(league_id, season):
    """
    Returns {teamId: ['First Last', ...]} for a given season,
    joining owners (IDs) with members (names).
    """
    members_map = _members_name_map_for_season(league_id, season)
    owners_map  = _owners_by_team_from_history(league_id, season)
    out = {}
    for tid, owner_ids in owners_map.items():
        out[tid] = [members_map.get(oid, oid) for oid in (owner_ids or [])]
    return out



# ================================
# Team managers (owner display names) — robust with espn_api fallback
# ================================
def _fetch_view_with_wrapper_then_http(view: str, year: int):
    """Try league._fetch_league(params={'view': view}) first, then raw GET."""
    try:
        data = league._fetch_league(params={"view": view})
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    base = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{LEAGUE_ID}"
    return GET(base, params={"view": view})

def fetch_team_managers(league_id: int, year: int) -> dict[int, list[str]]:
    """
    Returns {teamId: [manager display names...]} using multiple strategies:
      A) mTeam + mMembers (wrapper-first, then HTTP)
      B) espn_api.Team.owner / Team.owners fallback
    """
    managers_by_team: dict[int, list[str]] = {}

    # ---------- A) Try API views (preferred) ----------
    raw_team = _fetch_view_with_wrapper_then_http("mTeam", year) or {}
    raw_members = _fetch_view_with_wrapper_then_http("mMembers", year) or {}

    # Build owner IDs per team (owners[] or primaryOwner)
    owners_map: dict[int, list[str]] = {}
    for t in (raw_team.get("teams") or []):
        tid = t.get("id")
        owners = list(t.get("owners", []) or [])
        primary = t.get("primaryOwner")
        if primary and primary not in owners:
            owners.append(primary)
        owners_map[tid] = owners

    # Map member ID -> display name
    members_map: dict[str, str] = {}
    for m in (raw_members.get("members") or []):
        oid = m.get("id")
        display = (
            m.get("displayName")
            or " ".join(x for x in [m.get("firstName"), m.get("lastName")] if x)
            or m.get("nickname")
            or m.get("email")
            or oid
        )
        if oid:
            members_map[oid] = display

    # Resolve names from owners_map + members_map
    if owners_map:
        for tid, owner_ids in owners_map.items():
            if owner_ids:
                managers_by_team[tid] = [members_map.get(oid, oid) for oid in owner_ids]

    # ---------- B) espn_api fallback if still missing/empty ----------
    # Many leagues expose a friendly string via t.owner or t.owners on the Team object.
    # We’ll only add these if we have nothing from (A) or to fill blanks.
    for team_obj in league.teams:
        tid = team_obj.team_id
        # espn_api exposes either a single string or a list (varies by version)
        fallbacks: list[str] = []
        # Try .owners (list-like)
        if hasattr(team_obj, "owners"):
            try:
                if isinstance(team_obj.owners, (list, tuple)):
                    fallbacks.extend([str(x) for x in team_obj.owners if x])
                elif team_obj.owners:
                    fallbacks.append(str(team_obj.owners))
            except Exception:
                pass
        # Try .owner (single string)
        if hasattr(team_obj, "owner"):
            try:
                if team_obj.owner:
                    fallbacks.append(str(team_obj.owner))
            except Exception:
                pass

        # If we already have names from (A), only fill empty
        if tid not in managers_by_team or not managers_by_team[tid]:
            if fallbacks:
                managers_by_team[tid] = fallbacks

    return managers_by_team


# ================================
# ---- Current season basics ----
# ================================
# League size & teams
team_count = getattr(league.settings, "team_count", None)
league_name = getattr(league.settings, "name", None) or getattr(league, "league_name", None)

# Teams (id & name), Records, PF/PA
teams_current = []
for t in league.teams:
    teams_current.append({
        "team_id": t.team_id,
        "name": t.team_name,
        "record": {
            "wins": int(getattr(t, "wins", 0) or 0),
            "losses": int(getattr(t, "losses", 0) or 0),
            "ties": int(getattr(t, "ties", 0) or 0),
        },
        "points_for": float(getattr(t, "points_for", 0.0) or 0.0),
        "points_against": float(getattr(t, "points_against", 0.0) or 0.0),
    })

# Attach manager display names to each team
try:
    managers_by_team = fetch_team_managers(LEAGUE_ID, YEAR)
except Exception:
    managers_by_team = {}

for team in teams_current:
    tid = team["team_id"]
    team["managers"] = managers_by_team.get(tid, [])

# --- Keep only first + last name for managers (handles dicts or stringified dicts) ---
import ast

def _to_full_name(x):
    # dict from mMembers
    if isinstance(x, dict):
        first = (x.get("firstName") or "").strip()
        last  = (x.get("lastName")  or "").strip()
        name = f"{first} {last}".strip()
        if name:
            return name
        # fallback if first/last missing
        return (x.get("displayName") or x.get("nickname") or "").strip()

    # string that looks like a dict -> parse safely
    if isinstance(x, str):
        try:
            d = ast.literal_eval(x)  # handles single quotes/True/False/None
            if isinstance(d, dict):
                return _to_full_name(d)
        except Exception:
            pass
        return x.strip()  # last resort: leave as-is

    # anything else -> string
    return str(x).strip()

for team in teams_current:
    raw_mgrs = team.get("managers", [])
    team["managers"] = [n for n in (_to_full_name(m) for m in raw_mgrs) if n]


# ================================
# ---- Final results (history) ----
# ================================
history = []
start_year = YEAR - 1
end_year = max(YEAR - HISTORY_YEARS, YEAR - 10)  # safety cap

def _history_from_wrapper(season):
    """Try using espn_api.League for the past season (most reliable)."""
    try:
        past = League(league_id=LEAGUE_ID, year=season, espn_s2=ESPN_S2, swid=SWID)
    except Exception:
        return None
    
    # resolve teamId -> manager names for this past season
    mgr_map = _manager_names_for_season(LEAGUE_ID, season)

    teams_hist = []
    # espn_api exposes team name & season totals; final rank may not always be available
    for t in past.teams:
        teams_hist.append({
            "teamId": t.team_id,
            "name": t.team_name,
            "wins": int(getattr(t, "wins", 0) or 0),
            "losses": int(getattr(t, "losses", 0) or 0),
            "ties": int(getattr(t, "ties", 0) or 0),
            "pointsFor": float(getattr(t, "points_for", 0.0) or 0.0),
            "pointsAgainst": float(getattr(t, "points_against", 0.0) or 0.0),
            # espn_api sometimes has final_standing; if not, we’ll leave it None
            "rankFinal": getattr(t, "final_standing", None),
        })
    return {"season": season, "teams": teams_hist}

def _history_from_http(season):
    """Fallback to leagueHistory endpoint if wrapper fails."""
    payload = GET(
        f"https://fantasy.espn.com/apis/v3/games/ffl/leagueHistory/{LEAGUE_ID}",
        params={"seasonId": season, "view": "mStandings"}
    )
    data = payload[0] if isinstance(payload, list) and payload else (payload if isinstance(payload, dict) else None)
    if not data:
        return None
    
    # resolve teamId -> manager names for this past season
    mgr_map = _manager_names_for_season(LEAGUE_ID, season)

    teams_hist = []
    for t in (data.get("teams") or []):
        loc = t.get("location") or ""
        nick = t.get("nickname") or ""
        name = (loc + " " + nick).strip() or nick or loc or f"Team {t.get('id')}"
        rec = ((t.get("record") or {}).get("overall") or {})
        teams_hist.append({
            "teamId": t.get("id"),
            "name": name,
            "wins": rec.get("wins", 0),
            "losses": rec.get("losses", 0),
            "ties": rec.get("ties", 0),
            "pointsFor": t.get("pointsFor", 0.0),
            "pointsAgainst": t.get("pointsAgainst", 0.0),
            "rankFinal": t.get("rankCalculatedFinal") if t.get("rankCalculatedFinal") is not None else t.get("playoffSeed"),
        })
    return {"season": season, "teams": teams_hist}

for season in range(start_year, end_year, -1):  # e.g., 2024, 2023, ...
    row = _history_from_wrapper(season)
    if row is None:
        row = _history_from_http(season)
    if row:
        history.append(row)


# ================================
# ---- Final JSON bundle ----
# ================================
bundle = {
    "meta": {
        "generated_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source": "espn_api + leagueHistory",
    },
    "league": {
        "league_id": LEAGUE_ID,
        "league_name": league_name,
        "season_year": YEAR,
        "team_count": team_count,
    },
    "teams_current": teams_current,   # Team IDs, names, record, PF, PA
    "history": history,               # Final results for previous seasons (rank, W/L/T, PF, PA)
}

# ================================
# ---- Save to disk ----
# ================================
os.makedirs("out", exist_ok=True)
out_path = "out/llm_basic.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(bundle, f, indent=2)
print(f"Wrote {out_path}")
