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

# Shared HTTP bits for raw endpoints
BASE = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{LEAGUE_ID}"
COOKIES = {}
if ESPN_S2: COOKIES["espn_s2"] = ESPN_S2
if SWID:    COOKIES["SWID"] = SWID
HEADERS = {"User-Agent": "Mozilla/5.0 (cfg-pull/1.0)"}

def GET(url, params=None, timeout=20):
    """Resilient GET with cookies & small backoff."""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, cookies=COOKIES, headers=HEADERS, timeout=timeout)
            if r.ok:
                return r.json()
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return {}

def fetch_league_view(view):
    # Try espn_api internal first
    try:
        data = league._fetch_league(params={"view": view})
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    # Fallback to raw HTTP
    return GET(BASE, params={"view": view})

def fetch_players_view(params):
    """Query /players endpoint (for FA/projections)."""
    url = BASE.replace(f"/leagues/{LEAGUE_ID}", "/players")
    return GET(url, params=params)

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
# We’ll try league endpoint with view=kona_player_info for all active players in league context.
# Also pull free agents separately below.
def pull_kona_players(scoring_period_id=None, limit=250, start_index=0):
    params = {"view": "kona_player_info"}
    if scoring_period_id:
        params["scoringPeriodId"] = scoring_period_id
    # Pagination support
    params["limit"] = limit
    params["startIndex"] = start_index
    return fetch_players_view(params)

current_spid = season_calendar.get("currentScoringPeriodId")
players_this_week = []
if current_spid:
    # Pull a few pages to cover most active players (starters + benches) in league
    # ESPN returns mixed pools; we’ll just take the first ~500
    page = 0
    while page < 3:
        data = pull_kona_players(scoring_period_id=current_spid, limit=250, start_index=page*250)
        arr = data if isinstance(data, list) else []
        if not arr:
            break
        players_this_week.extend(arr)
        if len(arr) < 250:
            break
        page += 1

def simplify_kona(player_obj):
    """Extract core projection / actual for this scoring period and season."""
    p = player_obj.get("player", {}) if isinstance(player_obj, dict) else {}
    info = {
        "id": p.get("id"),
        "fullName": p.get("fullName"),
        "defaultPositionId": p.get("defaultPositionId"),
        "defaultPosition": POS_ID_MAP.get(p.get("defaultPositionId"), "UNK"),
        "proTeamId": p.get("proTeamId"),
        "proTeam": NFL_TEAM_ABBR.get(p.get("proTeamId"), None),
        "injuryStatus": p.get("injuryStatus"),
        "eligibleSlots": [LINEUP_SLOT_MAP.get(sid, str(sid)) for sid in (p.get("eligibleSlots") or [])],
        "ownership": player_obj.get("ownership", {}),
    }
    # stats array has elements with statSourceId (0=actual,1=projected) and scoringPeriodId
    sp = {"projected": None, "actual": None, "season_proj_total": None}
    for st in (player_obj.get("stats") or []):
        src = st.get("statSourceId")    # 0 actual, 1 projected
        spid = st.get("scoringPeriodId")
        if current_spid and spid == current_spid:
            if src == 1 and sp["projected"] is None:
                sp["projected"] = st.get("appliedStatTotal")
            if src == 0 and sp["actual"] is None:
                sp["actual"] = st.get("appliedStatTotal")
        # season-level projection totals sometimes come as scoringPeriodId = 0 or None with statSplitTypeId=1
        if src == 1 and (spid in (0, None)) and sp["season_proj_total"] is None:
            sp["season_proj_total"] = st.get("appliedStatTotal")
    info["this_week"] = {
        "projected": sp["projected"],
        "actual": sp["actual"]
    }
    info["season_proj_total"] = sp["season_proj_total"]
    # approximate ROS = season_proj_total - sum(actuals up to now); needs full season arrays to be perfect.
    info["ros_proj_approx"] = None
    return info

projections_this_week = [simplify_kona(p) for p in players_this_week]

# ================================
# Free agents / waivers (market depth & replacement level)
# ================================
def pull_free_agents_by_pos(pos, size):
    try:
        fa = league.free_agents(pos, size=size)  # espn_api convenience
    except Exception:
        return []
    out = []
    for pl in fa:
        item = {
            "name": getattr(pl, "name", None),
            "playerId": getattr(pl, "playerId", None) if hasattr(pl, "playerId") else getattr(pl, "id", None),
            "defaultPosition": getattr(pl, "position", None),
            "nfl_team": getattr(pl, "proTeam", None),
            "percent_owned": getattr(pl, "percent_owned", None),
            "projected_points": getattr(pl, "projected_points", None),
            "injury_status": getattr(pl, "injuryStatus", None) if hasattr(pl, "injuryStatus") else None
        }
        out.append(item)
    return out

FREE_AGENT_POS = ["QB","RB","WR","TE","K","D/ST"]
free_agents = {}
for pos in FREE_AGENT_POS:
    free_agents[pos] = pull_free_agents_by_pos(pos, FA_MAX_PER_POS)

# ================================
# Transactions / WA/FAAB (best-effort)
# ================================
transactions = []
try:
    # espn_api has recent_activity; combine with raw mTransactions2 if available
    acts = league.recent_activity(50)
    for a in acts:
        transactions.append({
            "actions": getattr(a, "actions", None),
            "date": getattr(a, "date", None),
            "type": getattr(a, "action_type", None),
            "team": getattr(a, "team", None)
        })
except Exception:
    pass

# supplement with raw if present
try:
    for t in (raw_trans.get("transactions") or []):
        transactions.append({
            "id": t.get("id"),
            "type": t.get("type"),
            "executionType": t.get("executionType"),
            "status": t.get("status"),
            "proposedDate": t.get("proposedDate"),
            "teamId": t.get("teamId"),
            "bidAmount": t.get("bidAmount"),
            "items": t.get("items")
        })
except Exception:
    pass

# ================================
# Teams payload (record, PF/PA, trends, SoS)
# ================================
# Derive PF/PA from standings_map when available; fallback to espn_api
pf_tot = {}
pa_tot = {}
record_map = {}
for t in teams:
    tid = t.team_id
    std = standings_map.get(tid, {})
    pf_tot[t.team_name] = float(std.get("pointsFor", getattr(t, "points_for", 0.0) or 0.0))
    pa_tot[t.team_name] = float(std.get("pointsAgainst", getattr(t, "points_against", 0.0) or 0.0))
    record_map[t.team_name] = {
        "wins": int(std.get("wins", getattr(t, "wins", 0) or 0)),
        "losses": int(std.get("losses", getattr(t, "losses", 0) or 0)),
        "ties": int(std.get("ties", getattr(t, "ties", 0) or 0))
    }

# past SoS = average opponent PF to date
opponents_by_team = {t.team_name: [] for t in teams}
for wk in range(1, week + 1):
    for g in scoreboards[wk]:
        hn, an = g.home_team.team_name, g.away_team.team_name
        opponents_by_team[hn].append(an)
        opponents_by_team[an].append(hn)
sos_avg = {}
for name, opps in opponents_by_team.items():
    sos_avg[name] = (sum(pf_tot.get(o, 0.0) for o in opps) / len(opps)) if opps else 0.0

# H2H vs current PF top/bottom 5
pf_sorted = sorted(pf_tot.items(), key=lambda x: x[1], reverse=True)
top5 = set([n for n, _ in pf_sorted[:5]])
bottom5 = set([n for n, _ in pf_sorted[-5:]]) if len(pf_sorted) >= 5 else set()

def h2h_groups(team_name):
    vtop = vbot = 0
    for wk in range(1, week + 1):
        for g in scoreboards[wk]:
            if team_name not in (g.home_team.team_name, g.away_team.team_name):
                continue
            opp = g.away_team.team_name if team_name == g.home_team.team_name else g.home_team.team_name
            won = ((g.home_score > g.away_score and team_name == g.home_team.team_name) or
                   (g.away_score > g.home_score and team_name == g.away_team.team_name))
            if won and opp in top5: vtop += 1
            if won and opp in bottom5: vbot += 1
    return {"vs_top5_wins": vtop, "vs_bottom5_wins": vbot}

teams_payload = []
for t in teams:
    name = t.team_name
    wp = weekly_points[name]
    teams_payload.append({
        "team_id": t.team_id,
        "name": name,
        "owners": owners_map.get(t.team_id, []),
        "record": record_map[name],
        "points_for_total": round(pf_tot[name], 2),
        "points_against_total": round(pa_tot[name], 2),
        "weekly_points": [round(x, 2) for x in wp],
        "recent_avg_last3": round(mean_last_n(wp, 3), 2),
        "stddev_points": round(safe_std(wp), 2),
        "sos_past_avg_opponent_pf": round(sos_avg[name], 2),
        "close_games": one_score[name],
        "head_to_head": h2h_groups(name),
        "allplay_expected_wins": round(allplay_expected_wins.get(name, 0.0), 3)
    })

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


