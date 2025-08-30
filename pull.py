import os, sys, json, datetime as dt
from statistics import pstdev
from espn_api.football import League

# ---------- Env & guards ----------
LEAGUE_ID = os.getenv("LEAGUE_ID")
YEAR = os.getenv("YEAR", "2025")

if not LEAGUE_ID or not LEAGUE_ID.strip():
    sys.exit("ERROR: LEAGUE_ID is empty. Provide it via env (GitHub secret or step env).")
try:
    LEAGUE_ID = int(LEAGUE_ID)
    YEAR = int(YEAR)
except ValueError:
    sys.exit("ERROR: LEAGUE_ID and YEAR must be integers.")

ESPN_S2 = os.getenv("ESPN_S2") or None  # only needed for private leagues
SWID    = os.getenv("SWID") or None

# ---------- League init ----------
try:
    league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)
except Exception as e:
    sys.exit(f"ERROR: Could not initialize League. Check LEAGUE_ID/YEAR and cookies. Details: {e}")

# ---------- Helpers ----------
def mean_last_n(values, n):
    if not values:
        return 0.0
    tail = values[-n:] if len(values) >= n else values
    return sum(tail) / len(tail)

def safe_std(values):
    return 0.0 if len(values) < 2 else pstdev(values)

# ---------- Determine week ----------
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

# ---------- Basic settings (espn_api wrapper) ----------
s = getattr(league, "settings", None)

# ---------- Raw pulls for rich config ----------
def fetch_raw(view):
    try:
        return league._fetch_league(params={"view": view})
    except Exception:
        return {}

raw_settings = fetch_raw("mSettings")       # rules, roster, schedule, status
raw_prosched = fetch_raw("proSchedule")     # NFL bye weeks (best-effort)

LINEUP_SLOT_MAP = {
    0:"QB",2:"RB",4:"WR",6:"TE",16:"D/ST",17:"K",20:"Bench",21:"IR",23:"FLEX",
    24:"WR/RB",25:"WR/TE",26:"RB/TE",27:"OP",28:"DE",29:"DT",30:"LB",31:"DL",
    32:"CB",33:"S",34:"DB",35:"DP"
}

settings_raw = raw_settings.get("settings", {}) or {}
status_raw   = raw_settings.get("status",   {}) or {}
scoring_raw  = settings_raw.get("scoringSettings", {}) or {}
roster_raw   = settings_raw.get("rosterSettings",  {}) or {}
schedule_raw = settings_raw.get("scheduleSettings",{}) or {}

# Bound our loops by the league's actual matchup periods if available
max_periods = schedule_raw.get("matchupPeriodCount") or getattr(s, "matchup_period_count", None) or 18
week = last_completed_week(max_periods)

# Preload current/this-week scoreboard (if any)
scoreboard_this_week = league.scoreboard(week=week) if week > 0 else []

# ---------- Human-friendly settings snapshot ----------
settings = {
    "league_name": getattr(s, "name", None) or getattr(league, "league_name", None),
    "team_count": getattr(s, "team_count", None),
    "division_count": getattr(s, "division_count", None) if hasattr(s, "division_count") else None,
    "has_divisions": bool(getattr(s, "divisions", None)) if hasattr(s, "divisions") else None,
    "scoring_type": getattr(s, "scoring_type", None),                 # e.g., PPR/STANDARD/HALF/H2H_POINTS
    "decimal_scoring": getattr(s, "decimal_scoring", None),
    "regular_season_matchup_count": getattr(s, "regular_season_matchup_count", None),
    "matchup_period_count": getattr(s, "matchup_period_count", None),
    "playoff_team_count": getattr(s, "playoff_team_count", None),
    "playoff_matchup_period_length": getattr(s, "playoff_matchup_period_length", None),
    "playoff_seed_rule": getattr(s, "playoff_seed_rule", None),
    "waiver_type": getattr(s, "waiver_type", None),
    "trade_deadline": getattr(s, "trade_deadline", None),
}

# ---------- Rich config (rules, roster slots, calendar, playoffs) ----------
scoring_rules = [{
    "statId": it.get("statId"),
    "points": it.get("points"),
    "isReverse": it.get("isReverse"),
    "isDecimal": scoring_raw.get("decimalScoring"),
    "name_hint": it.get("name")
} for it in (scoring_raw.get("scoringItems") or [])]

lineup_counts_raw = roster_raw.get("lineupSlotCounts", {}) or {}
lineup_slots = {
    str(slot): {"count": cnt, "slot_name": LINEUP_SLOT_MAP.get(int(slot), f"SLOT_{slot}")}
    for slot, cnt in lineup_counts_raw.items()
}

season_calendar = {
    "currentScoringPeriodId": status_raw.get("currentScoringPeriodId"),
    "firstScoringPeriodId":   status_raw.get("firstScoringPeriodId"),
    "finalScoringPeriodId":   status_raw.get("finalScoringPeriodId"),
}

bye_weeks = {}
try:
    teams_sched = (raw_prosched.get("proSchedule", {}) or {}).get("teams", []) or []
    for t in teams_sched:
        abbr = t.get("abbrev") or t.get("id")
        if abbr:
            bye_weeks[str(abbr)] = t.get("byeWeeks") or []
except Exception:
    bye_weeks = {}

playoffs = {
    "playoffTeamCount":            schedule_raw.get("playoffTeamCount"),
    "playoffSeedingRule":          schedule_raw.get("playoffSeedingRule"),
    "playoffMatchupPeriodLength":  schedule_raw.get("playoffMatchupPeriodLength"),
    "matchupPeriodCount":          schedule_raw.get("matchupPeriodCount"),
    "regularSeasonMatchupCount":   getattr(s, "regular_season_matchup_count", None),
    "playoffByeCount":             schedule_raw.get("playoffByeCount"),
}

trade_deadline_iso = None
td_ms = settings.get("trade_deadline")
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

# ---------- Cache scoreboards once (speed) ----------
scoreboards = {}
if week > 0:
    for wk in range(1, week + 1):
        scoreboards[wk] = league.scoreboard(week=wk)

# ---------- Team-level aggregates ----------
teams = league.teams
team_names = [t.team_name for t in teams]

pf_tot = {t.team_name: float(getattr(t, "points_for", 0.0)) for t in teams}
pa_tot = {t.team_name: float(getattr(t, "points_against", 0.0)) for t in teams}

# Weekly points up to completed week (from cache)
weekly_points = {name: [] for name in team_names}
for wk in range(1, week + 1):
    for g in scoreboards[wk]:
        weekly_points[g.home_team.team_name].append(float(g.home_score))
        weekly_points[g.away_team.team_name].append(float(g.away_score))

# Opponents faced (for simple SoS)
opponents_by_team = {name: [] for name in team_names}
for wk in range(1, week + 1):
    for g in scoreboards[wk]:
        hn, an = g.home_team.team_name, g.away_team.team_name
        opponents_by_team[hn].append(an)
        opponents_by_team[an].append(hn)

# Strength of Schedule = average opponent PF (to date)
sos_avg = {}
for name, opps in opponents_by_team.items():
    sos_avg[name] = (sum(pf_tot.get(o, 0.0) for o in opps) / len(opps)) if opps else 0.0

# One-score game record (<= 10 pts), decided games only
one_score = {name: {"wins": 0, "losses": 0} for name in team_names}
for wk in range(1, week + 1):
    for g in scoreboards[wk]:
        if getattr(g, "winner", None) is not None:
            margin = abs(g.home_score - g.away_score)
            if margin <= 10:
                winner = g.home_team.team_name if g.home_score > g.away_score else g.away_team.team_name
                loser  = g.away_team.team_name if winner == g.home_team.team_name else g.home_team.team_name
                one_score[winner]["wins"]  += 1
                one_score[loser]["losses"] += 1

# PF ranks for head-to-head vs top/bottom
pf_sorted = sorted(pf_tot.items(), key=lambda x: x[1], reverse=True)
top5 = set([n for n, _ in pf_sorted[:5]])
bottom5 = set([n for n, _ in pf_sorted[-5:]]) if len(pf_sorted) >= 5 else set()

def h2h_vs_groups(team_name):
    vtop = vbot = 0
    for wk in range(1, week + 1):
        for g in scoreboards[wk]:
            if team_name not in (g.home_team.team_name, g.away_team.team_name):
                continue
            opp = g.away_team.team_name if team_name == g.home_team.team_name else g.home_team.team_name
            won = ((g.home_score > g.away_score and team_name == g.home_team.team_name) or
                   (g.away_score > g.home_score and team_name == g.away_team.team_name))
            if won and opp in top5:
                vtop += 1
            if won and opp in bottom5:
                vbot += 1
    return {"vs_top5_wins": vtop, "vs_bottom5_wins": vbot}

# Build teams payload
teams_payload = []
for t in teams:
    name = t.team_name
    wp = weekly_points[name]
    teams_payload.append({
        "name": name,
        "owner": getattr(t, "owner", None),
        "record": {
            "wins": int(getattr(t, "wins", 0)),
            "losses": int(getattr(t, "losses", 0)),
            "ties": int(getattr(t, "ties", 0)) if hasattr(t, "ties") else 0
        },
        "points_for_total": round(pf_tot[name], 2),
        "points_against_total": round(pa_tot[name], 2),
        "weekly_points": [round(x, 2) for x in wp],
        "recent_avg_last3": round(mean_last_n(wp, 3), 2),
        "stddev_points": round(safe_std(wp), 2),
        "sos_avg_opponent_pf": round(sos_avg[name], 2),
        "close_games": {
            "one_score_wins": one_score[name]["wins"],
            "one_score_losses": one_score[name]["losses"]
        },
        "head_to_head": h2h_vs_groups(name),
        "notes": ""
    })

# ---------- This week's games (winner-safe) ----------
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
        "home": home,
        "home_score": round(hs, 2),
        "away": away,
        "away_score": round(as_, 2),
        "winner": winner,
        "margin": round(margin, 2) if margin is not None else None,
        "decided": decided
    })

# ---------- Derived summary ----------
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

# ---------- Final JSON bundle ----------
bundle = {
    "meta": {
        "generated_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "season_year": YEAR,
        "week_completed": week,
        "source": "espn_api"
    },
    "config": config,      # rich rules/slots/calendar/playoffs
    "settings": settings,  # human-friendly snapshot
    "teams": teams_payload,
    "games_this_week": games_payload,
    "derived": derived
}

# ---------- Save ----------
os.makedirs("out", exist_ok=True)
with open("out/llm_input.json", "w", encoding="utf-8") as f:
    json.dump(bundle, f, indent=2)
print("Wrote out/llm_input.json")

