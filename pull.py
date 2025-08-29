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

# ---------- Determine week & pull boards ----------
week = last_completed_week()
scoreboard_this_week = league.scoreboard(week=week)

# ---------- League settings (Option A via espn_api) ----------
s = getattr(league, "settings", None)

settings = {
    # Names
    "league_name": getattr(s, "name", None) or getattr(league, "league_name", None),
    # Structure
    "team_count": getattr(s, "team_count", None),
    "division_count": getattr(s, "division_count", None) if hasattr(s, "division_count") else None,
    "has_divisions": bool(getattr(s, "divisions", None)) if hasattr(s, "divisions") else None,
    # Scoring
    "scoring_type": getattr(s, "scoring_type", None),               # e.g., "PPR", "STANDARD"
    "decimal_scoring": getattr(s, "decimal_scoring", None),
    # Schedule / Playoffs
    "regular_season_matchup_count": getattr(s, "regular_season_matchup_count", None),
    "matchup_period_count": getattr(s, "matchup_period_count", None),  # often same as reg-season weeks
    "playoff_team_count": getattr(s, "playoff_team_count", None),
    "playoff_matchup_period_length": getattr(s, "playoff_matchup_period_length", None),
    "playoff_seed_rule": getattr(s, "playoff_seed_rule", None),
    # Transactions (handy context)
    "waiver_type": getattr(s, "waiver_type", None),
    "trade_deadline": getattr(s, "trade_deadline", None),
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
