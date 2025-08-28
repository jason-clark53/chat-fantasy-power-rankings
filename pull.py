import os, json, datetime as dt
import pandas as pd
from espn_api.football import League

LEAGUE_ID = int(os.getenv("LEAGUE_ID"))
YEAR = int(os.getenv("YEAR", "2025"))
ESPN_S2 = os.getenv("ESPN_S2") or None
SWID = os.getenv("SWID") or None

league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)

# --- determine last "completed" week robustly
def last_completed_week():
    # Try weeks 1..18 and pick the max that has at least one decided game
    last_done = 1
    for wk in range(1, 19):
        try:
            games = league.scoreboard(week=wk)
        except Exception:
            continue
        if games and any(getattr(g, "winner", None) is not None for g in games):
            last_done = wk
    return last_done

week = last_completed_week()

# --- standings snapshot
stand_rows = []
for t in league.teams:
    stand_rows.append({
        "team": t.team_name,
        "owner": getattr(t, "owner", None),
        "wins": t.wins, "losses": t.losses, "ties": getattr(t, "ties", 0),
        "points_for": round(t.points_for, 2),
        "points_against": round(t.points_against, 2),
    })
stand = pd.DataFrame(stand_rows).sort_values(
    ["wins", "points_for", "points_against"], ascending=[False, False, True]
).reset_index(drop=True)
stand["rank"] = stand.index + 1

# --- weekly games
games = []
scoreboard = league.scoreboard(week=week)
for g in scoreboard:
    games.append({
        "week": week,
        "home": g.home_team.team_name,
        "home_score": round(g.home_score, 2),
        "away": g.away_team.team_name,
        "away_score": round(g.away_score, 2),
        "winner": (g.home_team.team_name if g.home_score > g.away_score
                   else g.away_team.team_name)
    })
games_df = pd.DataFrame(games)

# --- superlatives (basic, tweak later as you like)
if not games_df.empty:
    games_df["margin"] = (games_df["home_score"] - games_df["away_score"]).abs()
    closest = games_df.loc[games_df["margin"].idxmin()].to_dict()
    high_game = games_df.loc[games_df[["home_score","away_score"]].max(axis=1).idxmax()].to_dict()

    # Determine team of the week by highest individual team score
    tot = []
    for _, r in games_df.iterrows():
        tot.append((r["home"], r["home_score"]))
        tot.append((r["away"], r["away_score"]))
    team_of_week = max(tot, key=lambda x: x[1])

else:
    closest, high_game, team_of_week = {}, {}, ("", 0.0)

# --- save artifacts
os.makedirs("out", exist_ok=True)
stand.to_csv(f"out/standings.csv", index=False)
games_df.to_csv(f"out/games_week_{week}.csv", index=False)
with open("out/meta.json","w") as f:
    json.dump({"week": week, "generated_at_utc": dt.datetime.utcnow().isoformat()}, f, indent=2)

# --- quick auto-article (Markdown)
def narrative():
    if games_df.empty:
        return "Quiet week (no completed games yet)."
    c = closest
    return (
        f"Week {week} delivered drama: the closest game was **{c.get('home')} vs {c.get('away')}** "
        f"separated by **{c.get('margin'):.2f}**. "
        f"**{team_of_week[0]}** posted the week’s top score at **{team_of_week[1]:.2f}**."
    )

lines = []
lines.append(f"# 🏈 Week {week} Power Rankings")
lines.append("")
lines.append(f"_{dt.datetime.utcnow().strftime('%B %d, %Y')} (auto-generated)_")
lines.append("")
lines.append(f"**Lead Story:** {narrative()}")
lines.append("")
lines.append("## Rankings")
for _, r in stand.sort_values('rank').iterrows():
    rec = f"{int(r.wins)}-{int(r.losses)}" + (f"-{int(r.ties)}" if int(r.ties) else "")
    lines.append(f"**{int(r['rank'])}. {r['team']} ({rec})** — PF: {r.points_for:.2f}, PA: {r.points_against:.2f}")
lines.append("")
if not games_df.empty:
    lines.append("## Superlatives")
    lines.append(f"- **Team of the Week:** {team_of_week[0]} ({team_of_week[1]:.2f})")
    lines.append(f"- **Closest Game:** {closest['home']} {closest['home_score']} – {closest['away']} {closest['away_score']} (Δ {closest['margin']:.2f})")

article_md = "\n".join(lines)
with open(f"out/power_rankings_week_{week}.md","w", encoding="utf-8") as f:
    f.write(article_md)

# --- image prompt suggestion (for your weekly custom graphic)
prompt = (
    f"Fantasy football Week {week}: feature the top team '{stand.iloc[0]['team']}' celebrating; "
    f"include a scoreboard hint of closest game margin ~{closest.get('margin', 6):.1f}."
)
with open(f"out/image_prompt_week_{week}.txt","w", encoding="utf-8") as f:
    f.write(prompt)
print(f"Generated week {week} artifacts in /out")
