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
    Returns {teamId: [manager full names]} where each name is "First Last".
    Falls back to displayName or nickname if no first/last.
    """
    base = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{league_id}"

    # 1) Pull owners per team
    owners_map: dict[int, list[str]] = {}
    raw_team = GET(base, params={"view": "mTeam"})
    for t in (raw_team.get("teams") or []):
        tid = t.get("id")
        owners_map[tid] = list(t.get("owners", []) or [])

    # 2) Member directory (id → clean full name)
    members_map: dict[str, str] = {}
    raw_members = GET(base, params={"view": "mMembers"})
    for m in (raw_members.get("members") or []):
        oid = m.get("id")
        if not oid:
            continue
        first = (m.get("firstName") or "").strip()
        last = (m.get("lastName") or "").strip()
        if first or last:
            clean = f"{first} {last}".strip()
        else:
            clean = m.get("displayName") or m.get("nickname") or oid
        members_map[oid] = clean

    # 3) Resolve owners → names
    managers_by_team: dict[int, list[str]] = {}
    for tid, owner_ids in owners_map.items():
        managers_by_team[tid] = [members_map.get(oid, oid) for oid in owner_ids]

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
