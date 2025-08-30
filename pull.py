import os, sys, json, datetime as dt, math, time
from statistics import pstdev
from collections import defaultdict
from espn_api.football import League
import requests

# ================================
# Env & basic guards
# ================================
LEAGUE_ID = os.getenv("LEAGUE_ID")
YEAR = int(os.getenv("YEAR", "2025"))
ESPN_S2 = os.getenv("ESPN_S2") or None
SWID    = os.getenv("SWID") or None

FA_MAX_PER_POS = int(os.getenv("FA_MAX_PER_POS", "150"))
HISTORY_YEARS  = int(os.getenv("HISTORY_YEARS", "3"))

if not LEAGUE_ID or not LEAGUE_ID.strip():
    sys.exit("ERROR: LEAGUE_ID is empty. Provide it via env (GitHub secret or step env).")
try:
    LEAGUE_ID = int(LEAGUE_ID)
except ValueError:
    sys.exit("ERROR: LEAGUE_ID must be an integer.")

# ================================
# Init league (espn_api for auth/session)
# ================================
try:
    league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
except Exception as e:
    sys.exit(f"ERROR: Could not initialize League. Check LEAGUE_ID/YEAR and cookies. Details: {e}")

s = getattr(league, "settings", None)

# --- ESPN-friendly headers & GET ---
import random

BASE = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{LEAGUE_ID}"
COOKIES = {}
if ESPN_S2: COOKIES["espn_s2"] = ESPN_S2
if SWID:    COOKIES["SWID"] = SWID

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (cfg-pull/1.1)",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://fantasy.espn.com/football/league?leagueId={LEAGUE_ID}",
    "x-fantasy-source": "kona-PWA",
    "x-fantasy-platform": "kona-PWA",
}

def GET(url, params=None, timeout=25):
    """Resilient GET with ESPN-friendly headers and cache-buster."""
    if params is None: params = {}
    params["_"] = str(random.randint(10**6, 10**7-1))
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, cookies=COOKIES, headers=COMMON_HEADERS, timeout=timeout)
            if r.ok:
                data = r.json()
                if data not in ({}, [], None):
                    return data
        except Exception:
            pass
        time.sleep(0.6 * (attempt + 1))
    return {}

def fetch_league_view(view):
    """Try espn_api internal first, then raw GET."""
    try:
        data = league._fetch_league(params={"view": view})
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    return GET(BASE, params={"view": view})


# ================================
# Helpers
# ================================
def mean_last_n(values, n):
    if not values:
        return 0.0
    tail = values[-n:] if len(values) >= n else values
    return sum(tail) / len(tail)

def safe_std(values):
    return 0.0 if len(values) < 2 else pstdev(values)

def last_completed_week(max_periods=18):
    """Highest week with at least one decided matchup; returns 0 if none decided yet."""
    last_done = 0
    for wk in range(1, max_periods + 1):
        try:
            games = league.scoreboard(week=wk)
        except Exception:
            continue
        if games and any(getattr(g, "winner", None) is not None for g in games):
            last_done = wk
    return last_done

LINEUP_SLOT_MAP = {
    0:"QB",2:"RB",4:"WR",6:"TE",16:"D/ST",17:"K",20:"Bench",21:"IR",23:"FLEX",
    24:"WR/RB",25:"WR/TE",26:"RB/TE",27:"OP",28:"DE",29:"DT",30:"LB",31:"DL",
    32:"CB",33:"S",34:"DB",35:"DP"
}
POS_ID_MAP = {  # defaultPositionId -> label (NFL)
    1:"QB", 2:"RB", 3:"WR", 4:"TE", 5:"K", 16:"D/ST", 7:"DL", 8:"LB", 9:"DB", 0:"UNK"
}
NFL_TEAM_ABBR = {  # partial helper (ESPN returns these in most views)
    0:"FA", 1:"ATL",2:"BUF",3:"CHI",4:"CIN",5:"CLE",6:"DAL",7:"DEN",8:"DET",9:"GB",
    10:"TEN",11:"IND",12:"KC",13:"LV",14:"LAR",15:"MIA",16:"MIN",17:"NE",18:"NO",
    19:"NYG",20:"NYJ",21:"PHI",22:"ARI",23:"PIT",24:"LAC",25:"SF",26:"SEA",27:"TB",
    28:"WSH",29:"CAR",30:"JAX",33:"BAL",34:"HOU"
}

# ================================
# Pull core views (config + schedule/standings/rosters/projections)
# ================================
raw_settings   = fetch_league_view("mSettings")     # rules/roster/schedule + status
raw_standings  = fetch_league_view("mStandings")    # standings & points
raw_teams      = fetch_league_view("mTeam")         # teams & owners
raw_schedule   = fetch_league_view("mSchedule")     # full schedule with teamIds per matchup
raw_rosters    = fetch_league_view("mRoster")       # team rosters with slots/eligibility
raw_box        = fetch_league_view("mMatchup")      # per-week box (if you later want optimal lineups)
raw_prosched   = fetch_league_view("proSchedule")   # NFL bye weeks
raw_trans      = fetch_league_view("mTransactions2")  # recent transactions (best effort)

def _short(v):
    if isinstance(v, dict): return len(v.keys())
    if isinstance(v, list): return len(v)
    return int(bool(v))

debug_views = {
    "mSettings": _short(raw_settings),
    "mStandings": _short(raw_standings),
    "mTeam": _short(raw_teams),
    "mSchedule": _short(raw_schedule),
    "mRoster": _short(raw_rosters),
    "proSchedule": _short(raw_prosched),
}
print("DEBUG view sizes:", debug_views)

# Bound loops by actual length if present
settings_raw = raw_settings.get("settings", {}) if isinstance(raw_settings, dict) else {}
status_raw   = raw_settings.get("status",   {}) if isinstance(raw_settings, dict) else {}
scoring_raw  = settings_raw.get("scoringSettings", {}) or {}
roster_raw   = settings_raw.get("rosterSettings",  {}) or {}
schedule_raw = settings_raw.get("scheduleSettings",{}) or {}

max_periods = schedule_raw.get("matchupPeriodCount") or getattr(s, "matchup_period_count", None) or 18
week = last_completed_week(max_periods)
scoreboard_this_week = league.scoreboard(week=week) if week > 0 else []

# Cache scoreboards to avoid repeated calls
scoreboards = {}
if week > 0:
    for wk in range(1, week + 1):
        scoreboards[wk] = league.scoreboard(week=wk)

# ================================
# League configuration (context)
# ================================
# Scoring rules
scoring_rules = [{
    "statId": it.get("statId"),
    "points": it.get("points"),
    "isReverse": it.get("isReverse"),
    "isDecimal": scoring_raw.get("decimalScoring"),
    "name_hint": it.get("name")
} for it in (scoring_raw.get("scoringItems") or [])]

# Roster slots
lineup_slots = {}
lsc = roster_raw.get("lineupSlotCounts", {}) or {}
for slot, cnt in lsc.items():
    slot_int = int(slot) if not isinstance(slot, int) else slot
    lineup_slots[str(slot)] = {"count": cnt, "slot_name": LINEUP_SLOT_MAP.get(slot_int, f"SLOT_{slot}")}

# Calendar
season_calendar = {
    "currentScoringPeriodId": status_raw.get("currentScoringPeriodId"),
    "firstScoringPeriodId":   status_raw.get("firstScoringPeriodId"),
    "finalScoringPeriodId":   status_raw.get("finalScoringPeriodId"),
}

# Bye weeks (NFL team -> [weeks])
bye_weeks = {}
try:
    teams_sched = (raw_prosched.get("proSchedule", {}) or {}).get("teams", []) or []
    for t in teams_sched:
        abbr = t.get("abbrev") or t.get("id")
        if abbr:
            bye_weeks[str(abbr)] = t.get("byeWeeks") or []
except Exception:
    pass

# Playoffs
playoffs = {
    "playoffTeamCount":            schedule_raw.get("playoffTeamCount") or getattr(s, "playoff_team_count", None),
    "playoffSeedingRule":          schedule_raw.get("playoffSeedingRule") or getattr(s, "playoff_seed_rule", None),
    "playoffMatchupPeriodLength":  schedule_raw.get("playoffMatchupPeriodLength") or getattr(s, "playoff_matchup_period_length", None),
    "matchupPeriodCount":          schedule_raw.get("matchupPeriodCount") or getattr(s, "matchup_period_count", None),
    "regularSeasonMatchupCount":   getattr(s, "regular_season_matchup_count", None),
    "playoffByeCount":             schedule_raw.get("playoffByeCount"),
}

trade_deadline_iso = None
td_ms = getattr(s, "trade_deadline", None)
if isinstance(td_ms, int):
    try:
        trade_deadline_iso = dt.datetime.utcfromtimestamp(td_ms/1000).isoformat() + "Z"
    except Exception:
        pass

config = {
    "scoring": {"type": getattr(s, "scoring_type", None), "rules": scoring_rules},
    "roster": {"lineup_slots": lineup_slots},
    "season_calendar": {**season_calendar, "bye_weeks": bye_weeks},
    "playoffs": playoffs,
    "ui": {"trade_deadline_iso": trade_deadline_iso}
}

# ================================
# Teams & standings (records, PF/PA, owners)
# ================================
teams = league.teams
team_by_id = {}
for t in teams:
    team_by_id[t.team_id] = t.team_name

# owners via mTeam view
owners_map = defaultdict(list)
try:
    for t in (raw_teams.get("teams") or []):
        tid = t.get("id")
        for o in t.get("owners", []) or []:
            owners_map[tid].append(o)
except Exception:
    pass

# standings via mStandings
standings_map = {}
try:
    for t in (raw_standings.get("teams") or []):
        tid = t.get("id")
        rec = (((t.get("record") or {}).get("overall") or {}) if t else {})
        standings_map[tid] = {
            "wins": rec.get("wins", 0),
            "losses": rec.get("losses", 0),
            "ties": rec.get("ties", 0),
            "pointsFor": t.get("pointsFor", 0.0),
            "pointsAgainst": t.get("pointsAgainst", 0.0)
        }
except Exception:
    pass

# ================================
# Schedule (past & future matchups)
# ================================
schedule_items = []
try:
    for it in (raw_schedule.get("schedule") or []):
        mp = it.get("matchupPeriodId")
        home = (it.get("home", {}) or {})
        away = (it.get("away", {}) or {})
        schedule_items.append({
            "matchupPeriodId": mp,
            "home": {
                "teamId": home.get("teamId"),
                "totalPoints": home.get("totalPoints"),
                "cumulativeScore": home.get("cumulativeScore")
            },
            "away": {
                "teamId": away.get("teamId"),
                "totalPoints": away.get("totalPoints"),
                "cumulativeScore": away.get("cumulativeScore")
            },
            "winner": it.get("winner")  # "HOME" / "AWAY" / "UNDECIDED"
        })
except Exception:
    pass

# ================================
# Weekly points history, one-score record, all-play expected wins
# ================================
team_names = [t.team_name for t in teams]
weekly_points = {name: [] for name in team_names}
allplay_expected_wins = {name: 0.0 for name in team_names}
one_score = {name: {"wins": 0, "losses": 0} for name in team_names}

for wk in range(1, week + 1):
    wk_scores = []
    for g in scoreboards[wk]:
        wk_scores.append((g.home_team.team_name, float(g.home_score)))
        wk_scores.append((g.away_team.team_name, float(g.away_score)))
        # one-score if decided and <=10 margin
        if getattr(g, "winner", None) is not None:
            margin = abs(g.home_score - g.away_score)
            if margin <= 10:
                winner = g.home_team.team_name if g.home_score > g.away_score else g.away_team.team_name
                loser  = g.away_team.team_name if winner == g.home_team.team_name else g.home_team.team_name
                one_score[winner]["wins"]  += 1
                one_score[loser]["losses"] += 1
    # populate weekly_points
    m = defaultdict(list)
    for name, sc in wk_scores:
        m[name].append(sc)
    for name in team_names:
        weekly_points[name].append(sum(m[name]) if m[name] else 0.0)
    # all-play expected wins (each team vs others)
    if wk_scores:
        scores_only = [sc for _, sc in wk_scores]
        for name, sc in wk_scores:
            beat = sum(1 for v in scores_only if sc > v)
            tie  = sum(1 for v in scores_only if sc == v) - 1  # minus self
            # expected wins share: wins + 0.5*ties, normalized by opponents count
            denom = max(1, len(scores_only) - 1)
            allplay_expected_wins[name] += (beat + 0.5 * max(0, tie)) / denom

# ================================
# Rosters (active + eligibility)
# ================================
rosters = []
try:
    for t in (raw_rosters.get("teams") or []):
        tid = t.get("id")
        tname = team_by_id.get(tid)
        entries = []
        for e in (t.get("roster") or {}).get("entries", []) or []:
            ppe = e.get("playerPoolEntry", {}) or {}
            pl  = ppe.get("player", {}) or {}
            entries.append({
                "playerId": pl.get("id"),
                "fullName": pl.get("fullName"),
                "defaultPositionId": pl.get("defaultPositionId"),
                "defaultPosition": POS_ID_MAP.get(pl.get("defaultPositionId"), "UNK"),
                "proTeamId": pl.get("proTeamId"),
                "proTeam": NFL_TEAM_ABBR.get(pl.get("proTeamId"), None),
                "eligibleSlots": [LINEUP_SLOT_MAP.get(sid, str(sid)) for sid in (pl.get("eligibleSlots") or [])],
                "lineupSlotId": e.get("lineupSlotId"),
                "lineupSlot": LINEUP_SLOT_MAP.get(e.get("lineupSlotId"), str(e.get("lineupSlotId"))),
                "injuryStatus": pl.get("injuryStatus"),
                "onIR": bool(e.get("injuryStatus") == "IR") or bool(e.get("lineupSlotId") == 21)
            })
        rosters.append({"teamId": tid, "team": tname, "entries": entries})
except Exception:
    pass

# ================================
# Player projections (this week + ROS best-effort)
# ================================
# --- Players (projections/ownership) via X-Fantasy-Filter ---
def players_post(filter_obj, scoring_period_id=None, start=0, limit=250):
    """
    Query /players with X-Fantasy-Filter (ESPN expects the filter in header).
    We still use GET but include 'x-fantasy-filter'; ESPN accepts this.
    """
    url = BASE.replace(f"/leagues/{LEAGUE_ID}", "/players")
    headers = dict(COMMON_HEADERS)
    headers["Content-Type"] = "application/json"
    headers["x-fantasy-filter"] = json.dumps(filter_obj)
    params = {}
    if scoring_period_id is not None:
        params["scoringPeriodId"] = scoring_period_id
    params["startIndex"] = start
    params["limit"] = limit

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, cookies=COOKIES, headers=headers, timeout=25)
            if r.ok:
                data = r.json()
                if isinstance(data, list) and data:
                    return data
        except Exception:
            pass
        time.sleep(0.6 * (attempt + 1))
    return []

def fetch_players_kona_all(scoring_period_id, max_pages=3):
    """Active players across ONTEAM/FA/WA with projections for this scoring period."""
    filt = {
        "players": {
            "filterStatus": {"value": ["ONTEAM", "FA", "WA"]},
            "filterSlotIds": {"value": list(LINEUP_SLOT_MAP.keys())},
            "sortAppliedStatTotal": {"sortPriority": 1, "sortAsc": False, "value": scoring_period_id},
            "filterRanksForScoringPeriodIds": {"value": [scoring_period_id]},
            "filterRanksForRankTypes": {"value": ["STANDARD"]},
            "filterRanksForSlotIds": {"value": [-1]},
        }
    }
    out = []
    for page in range(max_pages):
        batch = players_post(filt, scoring_period_id=scoring_period_id, start=page*250, limit=250)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 250:
            break
    return out

def simplify_kona(player_obj, current_spid):
    p = player_obj.get("player", {}) if isinstance(player_obj, dict) else {}
    info = {
        "id": p.get("id"),
        "fullName": p.get("fullName"),
        "defaultPositionId": p.get("defaultPositionId"),
        "defaultPosition": POS_ID_MAP.get(p.get("defaultPositionId"), "UNK"),
        "proTeamId": p.get("proTeamId"),
        "proTeam": NFL_TEAM_ABBR.get(p.get("proTeamId")),
        "injuryStatus": p.get("injuryStatus"),
        "eligibleSlots": [LINEUP_SLOT_MAP.get(sid, str(sid)) for sid in (p.get("eligibleSlots") or [])],
        "ownership": player_obj.get("ownership", {}),
    }
    proj = actual = season_proj_total = None
    for st in (player_obj.get("stats") or []):
        src = st.get("statSourceId")    # 0 actual, 1 projected
        spid = st.get("scoringPeriodId")
        if spid == current_spid:
            if src == 1 and proj is None:
                proj = st.get("appliedStatTotal")
            if src == 0 and actual is None:
                actual = st.get("appliedStatTotal")
        if src == 1 and (spid in (0, None)) and season_proj_total is None:
            season_proj_total = st.get("appliedStatTotal")
    info["this_week"] = {"projected": proj, "actual": actual}
    info["season_proj_total"] = season_proj_total
    info["ros_proj_approx"] = None
    return info

# Build current scoring period players
current_spid = season_calendar.get("currentScoringPeriodId")
projections_this_week = []
if current_spid:
    raw_players = fetch_players_kona_all(current_spid, max_pages=3)
    projections_this_week = [simplify_kona(p, current_spid) for p in raw_players]

# ================================
# Free agents / waivers (market depth & replacement level)
# ================================
def pull_free_agents_by_pos_filter(pos_label, scoring_period_id, size=150):
    rev = {v:k for k,v in POS_ID_MAP.items()}
    pos_id = rev.get(pos_label)
    filt = {
        "players": {
            "filterStatus": {"value": ["FA", "WA"]},
            **({"filterDefaultPositionIds": {"value": [pos_id]}} if pos_id else {}),
            "sortAppliedStatTotal": {"sortPriority": 1, "sortAsc": False, "value": scoring_period_id or 0},
            "filterRanksForScoringPeriodIds": {"value": [scoring_period_id or 0]},
            "filterRanksForRankTypes": {"value": ["STANDARD"]},
            "filterRanksForSlotIds": {"value": [-1]},
        }
    }
    out, pulled, start = [], 0, 0
    while pulled < size:
        batch = players_post(filt, scoring_period_id=scoring_period_id, start=start, limit=min(250, size - pulled))
        if not batch: break
        for p in batch:
            pl = (p.get("player") or {})
            item = {
                "playerId": pl.get("id"),
                "name": pl.get("fullName"),
                "defaultPosition": POS_ID_MAP.get(pl.get("defaultPositionId")),
                "nfl_team": NFL_TEAM_ABBR.get(pl.get("proTeamId")),
                "percent_owned": (p.get("ownership") or {}).get("percentOwned"),
                "injury_status": pl.get("injuryStatus")
            }
            proj = None
            for st in (p.get("stats") or []):
                if st.get("statSourceId") == 1 and st.get("scoringPeriodId") == scoring_period_id:
                    proj = st.get("appliedStatTotal"); break
            item["projected_points"] = proj
            out.append(item)
        pulled += len(batch)
        start += len(batch)
        if len(batch) < 250: break
    return out

FREE_AGENT_POS = ["QB","RB","WR","TE","K","D/ST"]
free_agents = {pos: pull_free_agents_by_pos_filter(pos, current_spid, size=FA_MAX_PER_POS) for pos in FREE_AGENT_POS}


# ================================
# This week's games (winner-safe)
# ================================
games_payload = []
for g in scoreboard_this_week:
    decided = getattr(g, "winner", None) is not None
    home = g.home_team.team_name
    away = g.away_team.team_name
    hs, as_ = float(g.home_score), float(g.away_score)
    margin = abs(hs - as_) if decided else None
    winner = home if (decided and hs > as_) else (away if decided else None)
    games_payload.append({
        "week": week,
        "home": home, "home_score": round(hs, 2),
        "away": away, "away_score": round(as_, 2),
        "winner": winner, "margin": round(margin, 2) if margin is not None else None,
        "decided": decided
    })

# ================================
# Derived league summary
# ================================
derived = {}
if week > 0 and games_payload and any(m["decided"] for m in games_payload):
    decided_games = [m for m in games_payload if m["decided"]]
    closest = min(decided_games, key=lambda x: x["margin"])
    single_scores = [(m["home"], m["home_score"]) for m in decided_games] + \
                    [(m["away"], m["away_score"]) for m in decided_games]
    top_team = max(single_scores, key=lambda x: x[1])
    total_scores_sum = sum(sum(v) for v in weekly_points.values())
    league_pf_avg = (total_scores_sum / (len(team_names) * week)) if (week > 0 and team_names) else 0.0
    derived = {
        "closest_game": closest,
        "highest_single_team_score": {"team": top_team[0], "points": round(top_team[1], 2)},
        "league_pf_avg_to_date": round(league_pf_avg, 2)
    }

# ================================
# Final results from previous years
# ================================
history = []
try:
    for past in range(YEAR-1, max(YEAR-HISTORY_YEARS, YEAR-10), -1):
        url = f"https://fantasy.espn.com/apis/v3/games/ffl/leagueHistory/{LEAGUE_ID}"
        data = GET(url, params={"seasonId": past, "view": "mStandings"})
        if isinstance(data, list) and data:
            # leagueHistory returns a list (single element per request)
            d = data[0]
        elif isinstance(data, dict) and data:
            d = data
        else:
            continue
        teams_hist = []
        for t in (d.get("teams") or []):
            rec = (((t.get("record") or {}).get("overall") or {}) if t else {})
            teams_hist.append({
                "teamId": t.get("id"),
                "location": t.get("location"),
                "nickname": t.get("nickname"),
                "owners": t.get("owners"),
                "wins": rec.get("wins", 0),
                "losses": rec.get("losses", 0),
                "ties": rec.get("ties", 0),
                "pointsFor": t.get("pointsFor", 0.0),
                "pointsAgainst": t.get("pointsAgainst", 0.0),
                "rankFinal": (t.get("rankCalculatedFinal") or t.get("playoffSeed"))
            })
        history.append({"season": past, "teams": teams_hist})
except Exception:
    pass

# ================================
# Output bundle
# ================================
human_settings = {
    "league_name": getattr(s, "name", None) or getattr(league, "league_name", None),
    "team_count": getattr(s, "team_count", None),
    "division_count": getattr(s, "division_count", None) if hasattr(s, "division_count") else None,
    "has_divisions": bool(getattr(s, "divisions", None)) if hasattr(s, "divisions") else None,
    "scoring_type": getattr(s, "scoring_type", None),
    "decimal_scoring": getattr(s, "decimal_scoring", None),
    "regular_season_matchup_count": getattr(s, "regular_season_matchup_count", None),
    "matchup_period_count": getattr(s, "matchup_period_count", None),
    "playoff_team_count": getattr(s, "playoff_team_count", None),
    "playoff_matchup_period_length": getattr(s, "playoff_matchup_period_length", None),
    "playoff_seed_rule": getattr(s, "playoff_seed_rule", None),
    "waiver_type": getattr(s, "waiver_type", None),
    "trade_deadline": getattr(s, "trade_deadline", None),
}

bundle = {
    "meta": {
        "generated_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "season_year": YEAR,
        "week_completed": week,
        "source": "espn_api+raw_v3"
    },
    "config": config,
    "settings": human_settings,
    "teams": teams_payload,                # teams & managers + record, PF/PA, trends, h2h, sos, all-play
    "standings_raw": raw_standings,        # full raw (optional debug)
    "schedule": schedule_items,            # full schedule (fuel for SoS past/future)
    "rosters": rosters,                    # current roster entries with slots & eligibility
    "player_projections": {
        "this_week": projections_this_week # per-player projected/actual for current scoring period; season_proj_total if present
    },
    "market": {
        "free_agents": free_agents         # FA pool by position (up to FA_MAX_PER_POS each)
    },
    "transactions": transactions,          # recent activity best-effort
    "games_this_week": games_payload,      # winner-safe
    "derived": derived,                    # closest game, top single score, league PF avg
    "history": history                     # final results for previous seasons (HISTORY_YEARS)
}

os.makedirs("out", exist_ok=True)
with open("out/llm_input.json", "w", encoding="utf-8") as f:
    json.dump(bundle, f, indent=2)
print("Wrote out/llm_input.json")


