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
def last_completed_week():
    """Pick the highest week that has at least one decided matchup."""
    last_done = 0
    # ESPN rarely goes past 18 weeks for fantasy, but iterate generously.
    for wk in range(1, 21):
        try:
            games = league.scoreboard(week=wk)
        except Exception:
            continue
        if games and any(getattr(g, "winner", None) is not None for g in games):
            last_done = wk
    return last_done or 1

def mean_last_n(values, n):
    if not values:
        return 0.0
    tail = values[-n:] if len(values) >= n else values
    return sum(tail) / len(tail)

def safe_std(values):
    return 0.0 if len(values) < 2 else pstdev(values)

def player_row(p):
    """Normalize player fields; handles missing attrs safely."""
    # Common attrs exposed by espn_api Player in football box scores
    name = getattr(p, "name", None)
    pos = getattr(p, "position", None)               # e.g., RB
    slot = getattr(p, "slot_position", None)         # e.g., RB, WR, BEN, FLEX
    nfl = getattr(p, "proTeam", None)                # e.g., DAL
    proj = getattr(p, "projected_points", None)
    pts  = getattr(p, "points", None)
    inj  = getattr(p, "injuryStatus", None) if hasattr(p, "injuryStatus") else None
    # Some espn_api versions expose 'injuryStatus' or similar under 'injuryStatus'/'injuryStatusDetail'
    return {
        "name": name,
        "position": pos,
        "lineup_slot": slot,
        "nfl_team": nfl,
        "projected_points": round(float(proj), 2) if isinstance(proj, (int, float)) else None,
        "actual_points": round(float(pts), 2) if isinstance(pts, (int, float)) else None,
        "injury_status": inj
    }

def lineup_block(box, home=True):
    team = box.home_team if home else box.away_team
    lineup = box.home_lineup if home else box.away_lineup
    return {
        "team": team.team_name,
        "players": [player_row(p) for p in lineup]
    }

# ---------- Determine week & pull boards ----------
week = last_completed_week()
scoreboard_this_week = league.scoreboard(week=week)

# ---------- League settings (Option A via espn_api) ----------
s = getattr(league, "settings", None)

# ------- RAW settings pull (for full detail not exposed by espn_api) -------
def fetch_raw(view):
    try:
        return league._fetch_league(params={"view": view})
    except Exception:
        return {}

raw_settings = fetch_raw("mSettings")          # scoring/roster/schedule + status
raw_prosched = fetch_raw("proSchedule")        # try to get NFL bye weeks (may be empty)

# Optional: readable names for lineup slot ids (ESPN numeric codes)
LINEUP_SLOT_MAP = {
    0:"QB", 2:"RB", 4:"WR", 6:"TE", 16:"D/ST", 17:"K",
    20:"Bench", 21:"IR", 23:"FLEX", 24:"WR/RB", 25:"WR/TE", 26:"RB/TE",
    27:"OP", 28:"DE", 29:"DT", 30:"LB", 31:"DL", 32:"CB", 33:"S", 34:"DB", 35:"DP"
}

# ------- Extract configuration pieces -------
settings_raw    = raw_settings.get("settings", {}) or {}
status_raw      = raw_settings.get("status",   {}) or {}
scoring_raw     = settings_raw.get("scoringSettings", {}) or {}
roster_raw      = settings_raw.get("rosterSettings",  {}) or {}
schedule_raw    = settings_raw.get("scheduleSettings",{}) or {}

# Scoring rules: map statId -> points (keep raw ids so math matches ESPN exactly)
scoring_rules = []
for item in scoring_raw.get("scoringItems", []) or []:
    # Common fields: statId, points, isReverse, multipliers, etc.
    scoring_rules.append({
        "statId": item.get("statId"),
        "points": item.get("points"),
        "isReverse": item.get("isReverse"),
        "isDecimal": scoring_raw.get("decimalScoring"),
        "name_hint": item.get("name"),  # some leagues include this; often None
    })

# Roster slots & lineup limits (slotId -> count) plus human names when available
lineup_counts_raw = roster_raw.get("lineupSlotCounts", {}) or {}
lineup_slots = {
    str(slot_id): {
        "count": cnt,
        "slot_name": LINEUP_SLOT_MAP.get(int(slot_id), f"SLOT_{slot_id}")
    }
    for slot_id, cnt in lineup_counts_raw.items()
}

# Season calendar: scoring period ids (used for week indexing / ROS math)
season_calendar = {
    "currentScoringPeriodId": status_raw.get("currentScoringPeriodId"),
    "firstScoringPeriodId":   status_raw.get("firstScoringPeriodId"),
    "finalScoringPeriodId":   status_raw.get("finalScoringPeriodId"),
}

# NFL bye weeks (best-effort from proSchedule; may be empty on some seasons/views)
bye_weeks = {}  # { "KC": [10], "DET": [5], ... }
try:
    teams = (raw_prosched.get("proSchedule", {}) or {}).get("teams", []) or []
    for t in teams:
        abbr = t.get("abbrev") or t.get("id")
        byes = t.get("byeWeeks") or []
        if abbr:
            bye_weeks[str(abbr)] = byes
except Exception:
    bye_weeks = {}

# Playoff format (weeks, team count, byes, matchup length, seeding rule)
playoffs = {
    "playoffTeamCount":              schedule_raw.get("playoffTeamCount"),
    "playoffSeedingRule":            schedule_raw.get("playoffSeedingRule"),
    "playoffMatchupPeriodLength":    schedule_raw.get("playoffMatchupPeriodLength"),
    "matchupPeriodCount":            schedule_raw.get("matchupPeriodCount"),
    "regularSeasonMatchupCount":     schedule_raw.get("matchupPeriodCount"),  # often same; keep both keys for clarity
    "playoffByeCount":               schedule_raw.get("playoffByeCount"),     # may be absent; OK if None
}

# ---------- League settings snapshot ----------
settings = {
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

# One consolidated configuration block (context for all math)
config = {
    "scoring": {
        "type": getattr(s, "scoring_type", None),     # PPR/Standard/Half
        "rules": scoring_rules                        # statId -> points, etc.
    },
    "roster": {
        "lineup_slots": lineup_slots                  # slotId -> {count, slot_name}
    },
    "season_calendar": {
        **season_calendar,
        "bye_weeks": bye_weeks                        # NFL team -> [weeks]
    },
    "playoffs": playoffs
}


# ---------- Team-level aggregates ----------
teams = league.teams
team_names = [t.team_name for t in teams]

# Total PF/PA to date
pf_tot = {t.team_name: float(getattr(t, "points_for", 0.0)) for t in teams}
pa_tot = {t.team_name: float(getattr(t, "points_against", 0.0)) for t in teams}

# Build weekly points per team up through the completed week
weekly_points = {name: [] for name in team_names}
for wk in range(1, week + 1):
    for g in league.scoreboard(week=wk):
        weekly_points[g.home_team.team_name].append(float(g.home_score))
        weekly_points[g.away_team.team_name].append(float(g.away_score))

# Opponents faced (for simple SoS)
opponents_by_team = {name: [] for name in team_names}
for wk in range(1, week + 1):
    for g in league.scoreboard(week=wk):
        hn, an = g.home_team.team_name, g.away_team.team_name
        opponents_by_team[hn].append(an)
        opponents_by_team[an].append(hn)

# Strength of Schedule = average opponent PF (to date)
sos_avg = {}
for name, opps in opponents_by_team.items():
    if opps:
        sos_avg[name] = sum(pf_tot.get(o, 0.0) for o in opps) / len(opps)
    else:
        sos_avg[name] = 0.0

# One-score game record (<= 10 pts margin)
one_score = {name: {"wins": 0, "losses": 0} for name in team_names}
for wk in range(1, week + 1):
    for g in league.scoreboard(week=wk):
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
        for g in league.scoreboard(week=wk):
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

# ---------- This week’s games ----------
games_payload = []
for g in scoreboard_this_week:
    margin = abs(g.home_score - g.away_score)
    games_payload.append({
        "week": week,
        "home": g.home_team.team_name,
        "home_score": round(float(g.home_score), 2),
        "away": g.away_team.team_name,
        "away_score": round(float(g.away_score), 2),
        "winner": g.home_team.team_name if g.home_score > g.away_score else g.away_team.team_name,
        "margin": round(float(margin), 2)
    })

# ---------- Derived summary ----------
derived = {}
if games_payload:
    closest = min(games_payload, key=lambda x: x["margin"])
    # highest single team score in the week
    single_scores = []
    for m in games_payload:
        single_scores.append((m["home"], m["home_score"]))
        single_scores.append((m["away"], m["away_score"]))
    top_team = max(single_scores, key=lambda x: x[1])
    league_pf_avg = 0.0
    total_scores_sum = sum(sum(v) for v in weekly_points.values())
    if week > 0 and len(team_names) > 0:
        league_pf_avg = total_scores_sum / (len(team_names) * week)

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
    "settings": settings,
    "teams": teams_payload,
    "games_this_week": games_payload,
    "derived": derived
}

# ---------- Save ----------
os.makedirs("out", exist_ok=True)
with open("out/llm_input.json", "w", encoding="utf-8") as f:
    json.dump(bundle, f, indent=2)
print("Wrote out/llm_input.json")
