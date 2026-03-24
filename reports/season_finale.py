"""Season Report: final standings, streaks, draft analysis, and season awards.

Pulls draft results, player scores, transactions, and schedule data
to generate a full season summary.

Usage:
    python season_report.py                    # CLI output
    python season_report.py --discord          # Post to Discord
    python season_report.py --dry-run          # Preview Discord embed JSON
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.fantrax_client import FantraxClient
from utils.stats import LOWER_IS_BETTER

LEAGUE_ID = os.environ.get("FANTRAX_LEAGUE_ID", "uo0es7lom23shg6b")

# Only consider picks from rounds 1 through this
MAX_ROUND = 10

# Minimum bust score (actual_rank - draft_pick) to count as a bust
MIN_BUST_SCORE = 150

# Number of steals/busts to show
TOP_PICKS = 3

# Shorten long team names for mobile formatting
SHORT_NAMES = {
    "Friedl Dee & Friedl Dum": "Friedl Dee",
}

# Number of regular season periods (playoffs follow after)
REG_SEASON_PERIODS = 20


def fetch_all_player_scores(client: FantraxClient) -> dict[str, dict]:
    """Fetch scores for ALL players, paginating to cover drafted players ranked beyond 500."""
    PAGE_SIZE = 500
    NUM_PAGES = 3
    score_map = {}
    score_idx = None
    rank_idx = None

    for page in range(1, NUM_PAGES + 1):
        data = client._call(
            "getPlayerStats",
            statusOrTeamFilter="ALL",
            maxResultsPerPage=str(PAGE_SIZE),
            pageNumber=str(page),
        )

        if score_idx is None:
            header = data.get("tableHeader", {}).get("cells", [])
            col_keys = [c.get("key", "") for c in header]
            score_idx = next((i for i, k in enumerate(col_keys) if k == "score"), None)
            rank_idx = next((i for i, k in enumerate(col_keys) if k == "rankOv"), None)

        rows = data.get("statsTable", [])
        if not rows:
            break

        for row in rows:
            scorer = row.get("scorer", {})
            sid = scorer.get("scorerId", "")
            if not sid:
                continue
            cells = row.get("cells", [])
            score = cells[score_idx].get("content", "") if score_idx is not None else ""
            rank = cells[rank_idx].get("content", "") if rank_idx is not None else ""
            score_map[sid] = {"score": score, "rank": rank, "name": scorer.get("name", "")}

        if len(rows) < PAGE_SIZE:
            break

    return score_map


def fetch_season_stats(client: FantraxClient) -> dict:
    """Fetch season hitting/pitching point leaders from SEASON_STATS view."""
    data = client._call("getStandings", view="SEASON_STATS")
    tables = data.get("tableList", [])

    def _parse_table(table: dict) -> list[tuple[str, float]]:
        results = []
        for row in table.get("rows", []):
            fc = row["fixedCells"]
            team = fc[1].get("content", "") if len(fc) > 1 else fc[0].get("content", "")
            pts = row["cells"][0].get("content", "")
            try:
                results.append((team, float(pts)))
            except (ValueError, TypeError):
                continue
        return results

    hitting = _parse_table(tables[4]) if len(tables) > 4 else []
    pitching = _parse_table(tables[5]) if len(tables) > 5 else []

    # Most balanced: best combined rank with smallest gap
    hit_ranks = {team: i + 1 for i, (team, _) in enumerate(hitting)}
    pit_ranks = {team: i + 1 for i, (team, _) in enumerate(pitching)}
    all_teams = set(hit_ranks) & set(pit_ranks)
    balanced = None
    if all_teams:
        balanced_team = min(all_teams, key=lambda t: (
            hit_ranks[t] + pit_ranks[t], abs(hit_ranks[t] - pit_ranks[t])
        ))
        balanced = (balanced_team, hit_ranks[balanced_team], pit_ranks[balanced_team])

    return {
        "top_offense": hitting[0] if hitting else None,
        "top_pitching": pitching[0] if pitching else None,
        "most_balanced": balanced,
    }


def fetch_schedule_data(client: FantraxClient) -> dict:
    """Fetch all schedule-derived stats: luck, streaks, biggest blowout, playoff results."""
    schedule = client.schedule()

    reg_season = [p for p in schedule[:REG_SEASON_PERIODS] if p["matchups"]]
    if not reg_season:
        return {}

    last_period = reg_season[-1]["period_num"]

    # --- Compute regular season records from schedule (standings API unreliable during playoffs) ---
    actual_records: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
    for period in reg_season:
        for m in period["matchups"]:
            aw, al = m["away_wins"], m["away_losses"]
            if aw > al:
                actual_records[m["away_team_name"]]["wins"] += 1
                actual_records[m["home_team_name"]]["losses"] += 1
            elif al > aw:
                actual_records[m["away_team_name"]]["losses"] += 1
                actual_records[m["home_team_name"]]["wins"] += 1
            else:
                actual_records[m["away_team_name"]]["ties"] += 1
                actual_records[m["home_team_name"]]["ties"] += 1

    standings_list = sorted(
        actual_records.items(),
        key=lambda x: (x[1]["wins"], -x[1]["losses"]),
        reverse=True,
    )

    # --- Luck (category-level: actual cat W-L-T vs expected cat W-L-T vs league avg) ---
    from stats import _vs_avg_category_record
    vs_avg_cats = _vs_avg_category_record(reg_season, last_period)

    # Accumulate actual category W-L-T from matchups
    actual_cat_records: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
    for period in reg_season:
        for m in period["matchups"]:
            for side in ("away", "home"):
                name = m[f"{side}_team_name"]
                actual_cat_records[name]["wins"] += m[f"{side}_wins"]
                actual_cat_records[name]["losses"] += m[f"{side}_losses"]
                actual_cat_records[name]["ties"] += m[f"{side}_ties"]

    luck = {}
    for name, rec in actual_cat_records.items():
        if name in vs_avg_cats:
            games_back = rec["wins"] - vs_avg_cats[name]["wins"]
            luck[name] = games_back

    sorted_luck = sorted(luck.items(), key=lambda x: x[1], reverse=True)
    luckiest = sorted_luck[0] if sorted_luck else None
    unluckiest = sorted_luck[-1] if sorted_luck else None

    # --- Season streaks ---
    history = defaultdict(list)
    for period in reg_season:
        for m in period["matchups"]:
            if m["away_wins"] > m["home_wins"]:
                history[m["away_team_name"]].append("W")
                history[m["home_team_name"]].append("L")
            elif m["home_wins"] > m["away_wins"]:
                history[m["home_team_name"]].append("W")
                history[m["away_team_name"]].append("L")
            else:
                history[m["away_team_name"]].append("T")
                history[m["home_team_name"]].append("T")

    def _longest_streak(results: list[str], streak_type: str) -> int:
        longest = 0
        current = 0
        for r in results:
            if r == streak_type:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest

    best_win_streak = None
    best_lose_streak = None
    for team, results in history.items():
        ws = _longest_streak(results, "W")
        if best_win_streak is None or ws > best_win_streak[1]:
            best_win_streak = (team, ws)
        ls = _longest_streak(results, "L")
        if best_lose_streak is None or ls > best_lose_streak[1]:
            best_lose_streak = (team, ls)

    # --- Biggest blowout of the season ---
    best_blowout = None
    for period in reg_season:
        for m in period["matchups"]:
            for side, opp in [("away", "home"), ("home", "away")]:
                w, l, t = m[f"{side}_wins"], m[f"{side}_losses"], m[f"{side}_ties"]
                net = w - l
                if best_blowout is None or net > best_blowout["net"]:
                    best_blowout = {
                        "winner": m[f"{side}_team_name"],
                        "loser": m[f"{opp}_team_name"],
                        "record": f"{w}-{l}-{t}",
                        "net": net,
                        "period": period["period_num"],
                    }

    # --- Final standings / playoff results ---
    reg_season_winner = None
    if standings_list:
        rsw_name = standings_list[0][0]
        rsw_rec = standings_list[0][1]
        reg_season_winner = (rsw_name, f"{rsw_rec['wins']}-{rsw_rec['losses']}-{rsw_rec['ties']}")

    # Biggest climber/faller: track each team's worst/best standing across the
    # season (ignoring first 4 weeks), then compare to final standing.
    IGNORE_WEEKS = 4
    final_ranks = {team: i + 1 for i, (team, _) in enumerate(standings_list)}
    # worst_rank = highest number (e.g. 12th), best_rank = lowest number (e.g. 1st)
    worst_rank: dict[str, int] = {}   # team -> worst (highest) rank seen
    best_rank: dict[str, int] = {}    # team -> best (lowest) rank seen
    running_records: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
    for week_num, period in enumerate(reg_season, 1):
        for m in period["matchups"]:
            aw, al = m["away_wins"], m["away_losses"]
            if aw > al:
                running_records[m["away_team_name"]]["wins"] += 1
                running_records[m["home_team_name"]]["losses"] += 1
            elif al > aw:
                running_records[m["away_team_name"]]["losses"] += 1
                running_records[m["home_team_name"]]["wins"] += 1
            else:
                running_records[m["away_team_name"]]["ties"] += 1
                running_records[m["home_team_name"]]["ties"] += 1
        if week_num <= IGNORE_WEEKS:
            continue
        week_sorted = sorted(
            running_records.items(),
            key=lambda x: (x[1]["wins"], -x[1]["losses"]),
            reverse=True,
        )
        for rank, (team, _) in enumerate(week_sorted, 1):
            if team not in worst_rank or rank > worst_rank[team][0]:
                worst_rank[team] = (rank, week_num)
            if team not in best_rank or rank < best_rank[team][0]:
                best_rank[team] = (rank, week_num)

    biggest_climber = None
    biggest_faller = None
    for team, final in final_ranks.items():
        # Climber: was at their worst rank, finished higher (lower number)
        if team in worst_rank:
            worst_r, worst_week = worst_rank[team]
            climb = worst_r - final  # positive = climbed
            if climb > 0 and (biggest_climber is None or climb > biggest_climber[1]):
                biggest_climber = (team, climb, worst_week, last_period)
        # Faller: was at their best rank, finished lower (higher number)
        if team in best_rank:
            best_r, best_week = best_rank[team]
            fall = best_r - final  # negative = fell
            if fall < 0 and (biggest_faller is None or fall < biggest_faller[1]):
                biggest_faller = (team, fall, best_week, last_period)

    # Champion: winner of the last playoff matchup + playoff record
    playoffs = schedule[REG_SEASON_PERIODS:]
    champion = None
    playoff_cats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
    for period in playoffs:
        if not period["matchups"]:
            continue
        for m in period["matchups"]:
            aw, al = m["away_wins"], m["away_losses"]
            num_cats = len(m.get("away_cats", {})) if m.get("away_cats") else aw + al
            at = num_cats - aw - al
            playoff_cats[m["away_team_name"]]["wins"] += aw
            playoff_cats[m["away_team_name"]]["losses"] += al
            playoff_cats[m["away_team_name"]]["ties"] += at
            playoff_cats[m["home_team_name"]]["wins"] += al
            playoff_cats[m["home_team_name"]]["losses"] += aw
            playoff_cats[m["home_team_name"]]["ties"] += at
    for period in reversed(playoffs):
        if not period["matchups"]:
            continue
        final = period["matchups"][0]
        if final["away_wins"] > final["home_wins"]:
            champ_name = final["away_team_name"]
        elif final["home_wins"] > final["away_wins"]:
            champ_name = final["home_team_name"]
        else:
            break
        pc = playoff_cats[champ_name]
        champion = (champ_name, f"{pc['wins']}-{pc['losses']}-{pc['ties']}")
        break

    # Extract season year from last period's date range (e.g. "Sep 15 - Sep 21, 2025")
    season_year = None
    for p in reversed(schedule):
        dr = p.get("date_range", "")
        year_match = re.search(r"(\d{4})", dr)
        if year_match:
            season_year = int(year_match.group(1))
            break

    return {
        "season_year": season_year,
        "luckiest": luckiest,
        "unluckiest": unluckiest,
        "longest_win_streak": best_win_streak,
        "longest_lose_streak": best_lose_streak,
        "biggest_blowout": best_blowout,
        "champion": champion,
        "reg_season_winner": reg_season_winner,
        "biggest_climber": biggest_climber,
        "biggest_faller": biggest_faller,
    }


def fetch_end_of_season_rosters(client: FantraxClient) -> dict[str, str]:
    """Fetch rosters at end of regular season (period 20). Returns {player_name: team_name}."""
    data = client._call("getTeamRosterInfo", period=str(REG_SEASON_PERIODS))
    teams = {ft["id"]: ft["name"] for ft in data.get("fantasyTeams", [])}

    def _parse_roster(data: dict, team_id: str) -> dict[str, str]:
        team_name = teams.get(team_id, team_id)
        roster = {}
        for table in data.get("tables", []):
            for row in table.get("rows", []):
                name = row.get("scorer", {}).get("name")
                if name:
                    roster[name] = team_name
        return roster

    first_team_id = data.get("displayedSelections", {}).get("displayedFantasyTeamId", "")
    all_rosters = _parse_roster(data, first_team_id)
    remaining = [tid for tid in teams if tid != first_team_id]

    def _fetch_team(tid):
        c = FantraxClient(client.league_id)
        d = c._call("getTeamRosterInfo", teamId=tid, period=str(REG_SEASON_PERIODS))
        return _parse_roster(d, tid)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for roster in pool.map(_fetch_team, remaining):
            all_rosters.update(roster)

    return all_rosters


def fetch_transaction_stats(client: FantraxClient) -> dict:
    """Fetch transaction counts per team (adds only, not drops)."""
    txns = client.transactions(count=2000)
    adds = [t for t in txns if t.get("added")]
    counts = Counter(t["team_name"] for t in adds)
    most = counts.most_common(1)[0] if counts else None

    # Compute per-day rate: calendar days from first to last add, minus days with zero league-wide adds
    per_day = None
    if most:
        all_dates = []
        for t in adds:
            m = re.match(r"\w+ (\w+ \d+, \d+)", t.get("date", ""))
            if m:
                all_dates.append(datetime.strptime(m.group(1), "%b %d, %Y").date())
        if all_dates:
            first, last = min(all_dates), max(all_dates)
            active_days = set(all_dates)
            calendar_days = (last - first).days + 1
            game_days = sum(1 for d in (first + timedelta(days=i) for i in range(calendar_days)) if d in active_days)
            per_day = most[1] / game_days

    return {"most_waiver_moves": most, "waiver_per_day": per_day}


def analyze_draft(draft_picks: list[dict], scores: dict[str, dict], end_rosters: dict[str, str]) -> dict:
    """Analyze draft picks and find busts, values, and waiver gems."""
    busts = []
    values = []
    team_totals: dict[str, float] = {}

    for pick in draft_picks:
        sid = pick["scorer_id"]
        score_info = scores.get(sid)

        # Accumulate total score per team (all rounds)
        if score_info and score_info.get("score"):
            try:
                score_val = float(score_info["score"])
            except (ValueError, TypeError):
                score_val = 0
            team_totals[pick["team_name"]] = team_totals.get(pick["team_name"], 0) + score_val

        if pick["round"] > MAX_ROUND:
            continue

        if not score_info or not score_info.get("rank"):
            continue

        try:
            actual_rank = int(score_info["rank"])
        except (ValueError, TypeError):
            continue

        expected_rank = pick["overall_pick"]
        bust_score = actual_rank - expected_rank

        pick_data = {
            **pick,
            "score": score_info.get("score", ""),
            "rank": actual_rank,
            "bust_score": bust_score,
        }

        if bust_score >= MIN_BUST_SCORE:
            busts.append(pick_data)

        values.append(pick_data)

    busts.sort(key=lambda x: x["bust_score"], reverse=True)
    values.sort(key=lambda x: x["bust_score"])

    # Best value pick (biggest outperformance)
    best_value = values[0] if values and values[0]["bust_score"] < 0 else None

    # Most predictable pick (closest to expected)
    predictable = min(values, key=lambda x: abs(x["bust_score"])) if values else None

    # Best pickup: highest-ranked undrafted player on a roster at end of regular season
    best_waiver = None
    drafted_names = {p["player_name"] for p in draft_picks}

    for name, team in end_rosters.items():
        if name in drafted_names:
            continue
        for sid, info in scores.items():
            if info.get("name") != name:
                continue
            try:
                rank = int(info["rank"])
                score = float(info["score"])
            except (ValueError, TypeError):
                break
            if best_waiver is None or rank < best_waiver["rank"]:
                best_waiver = {"name": name, "rank": rank, "score": score, "team": team}
            break

    team_counts = Counter(b["team_name"] for b in busts)

    sorted_teams = sorted(team_totals.items(), key=lambda x: x[1], reverse=True)
    best_draft = sorted_teams[0] if sorted_teams else None
    worst_draft = sorted_teams[-1] if sorted_teams else None

    steals = [v for v in values if v["bust_score"] < 0][:TOP_PICKS]

    return {
        "busts": busts,
        "steals": steals,
        "team_bust_counts": team_counts,
        "best_draft": best_draft,
        "worst_draft": worst_draft,
        "best_value": best_value,
        "most_predictable": predictable,
        "best_waiver": best_waiver,
    }


def _build_report(analysis: dict, markdown: bool = False) -> str:
    """Build season report. If markdown=True, includes Discord formatting."""
    busts = analysis["busts"][:TOP_PICKS]
    steals = analysis.get("steals", [])
    b = "**" if markdown else ""  # bold wrapper
    lines = []

    def _pick_table(picks):
        if markdown:
            return "\n".join(
                f"**{i}.** **{p['player_name']}** ({p['team_name']}) - Pick #{p['overall_pick']} > #{p['rank']}"
                for i, p in enumerate(picks, 1)
            )
        tw = max(4, max(len(p["team_name"]) for p in picks))
        pw = max(6, max(len(p["player_name"]) for p in picks))
        tbl = []
        tbl.append(f"{'#':<3} {'Team':<{tw}}  {'Player':<{pw}}  {'Pick':>5}  {'Rank':>5}")
        tbl.append("-" * (3 + 2 + tw + 2 + pw + 2 + 5 + 2 + 5))
        for i, p in enumerate(picks, 1):
            tbl.append(f"{i:<3} {p['team_name']:<{tw}}  {p['player_name']:<{pw}}  "
                       f"#{p['overall_pick']:>4}  #{p['rank']:>4}")
        return "\n".join(tbl)

    def _header(title):
        if markdown:
            lines.append(f"\n### {title}")
        else:
            lines.append("")
            lines.append("=" * 60)
            lines.append(f"  {title}")
            lines.append("=" * 60)

    def _awards(awards: list[tuple[str, str, str]]):
        if markdown:
            for emoji, label, value in awards:
                team, _, stat = value.partition("\n")
                line = f"{emoji} **{label}:** {team}"
                if stat:
                    line += f" - {stat}"
                lines.append(line)
        else:
            max_label = max(len(label) for _, label, _ in awards)
            for emoji, label, value in awards:
                padded = f"{label}:".ljust(max_label + 1)
                team, _, stat = value.partition("\n")
                lines.append(f"{emoji} {padded} {team}")
                if stat:
                    lines.append(f"{'':>{max_label + 5}}{stat}")

    # Title
    year = analysis.get("season_year", "")
    if markdown:
        lines.append(f"## {year} Season Report")
    else:
        lines.append(f"{year} Season Report")

    # Final Standings
    _header("Final Standings")
    standings_group = []
    champion = analysis.get("champion")
    if champion:
        standings_group.append(("\U0001f3c6", "Champion", f"{champion[0]}\n{champion[1]} playoffs"))
    rsw = analysis.get("reg_season_winner")
    if rsw:
        standings_group.append(("\U0001f451", "Regular Season Winner", f"{rsw[0]}\n{rsw[1]}"))
    climber = analysis.get("biggest_climber")
    if climber:
        standings_group.append(("\U0001f4c8", "Biggest Climber", f"{climber[0]}\n+{climber[1]} spots (Week {climber[2]}-{climber[3]})"))
    faller = analysis.get("biggest_faller")
    if faller:
        standings_group.append(("\U0001f4c9", "Biggest Faller", f"{faller[0]}\n{faller[1]} spots (Week {faller[2]}-{faller[3]})"))
    if standings_group:
        _awards(standings_group)

    # Season Streaks & Blowouts
    _header("Season Streaks & Blowouts")
    streak_group = []
    ws = analysis.get("longest_win_streak")
    if ws:
        streak_group.append(("\U0001f525", "Longest Win Streak", f"{ws[0]}\n{ws[1]}W"))
    ls = analysis.get("longest_lose_streak")
    if ls:
        streak_group.append(("\U0001f4c9", "Longest Losing Streak", f"{ls[0]}\n{ls[1]}L"))
    blowout = analysis.get("biggest_blowout")
    if blowout:
        streak_group.append(("\U0001f480", "Biggest Blowout", f"{blowout['winner']} over {blowout['loser']}\n{blowout['record']} (Week {blowout['period']})"))
    if streak_group:
        _awards(streak_group)

    # Best picks
    if steals:
        _header("\U0001f48e Best Draft Picks")
        lines.append(_pick_table(steals))

    # Worst picks
    if busts:
        _header("\U0001f4a9 Worst Draft Picks")
        lines.append(_pick_table(busts))

    # Draft Awards
    _header("Draft Awards")
    draft_group = []
    best = analysis["best_draft"]
    if best:
        draft_group.append(("\U0001f4c8", "Best Overall Draft", f"{best[0]}\n{best[1]:.1f} total score"))
    worst_draft = analysis["worst_draft"]
    if worst_draft:
        draft_group.append(("\U0001f4c9", "Worst Overall Draft", f"{worst_draft[0]}\n{worst_draft[1]:.1f} total score"))
    if draft_group:
        _awards(draft_group)

    # Waiver Wire
    _header("Waiver Wire")
    waiver_group = []
    bw = analysis.get("best_waiver")
    if bw:
        waiver_group.append(("\U0001f48e", "Best Pickup", f"{bw['team']}\n{bw['name']} (ranked #{bw['rank']})"))
    wm = analysis.get("most_waiver_moves")
    if wm:
        wpd = analysis.get("waiver_per_day")
        detail = f"{wm[1]} adds"
        if wpd:
            detail += f" ({wpd:.1f}/day)"
        waiver_group.append(("\U0001f504", "Most Pickups", f"{wm[0]}\n{detail}"))
    if waiver_group:
        _awards(waiver_group)

    # Season Stats
    _header("Season Stats")
    stat_group = []
    top_offense = analysis.get("top_offense")
    if top_offense:
        stat_group.append(("\U0001f4aa", "Top Offense", f"{top_offense[0]}\n{top_offense[1]:g} pts"))
    top_pitching = analysis.get("top_pitching")
    if top_pitching:
        stat_group.append(("\u26be", "Top Pitching", f"{top_pitching[0]}\n{top_pitching[1]:g} pts"))
    balanced = analysis.get("most_balanced")
    if balanced:
        team, hit_rank, pit_rank = balanced
        stat_group.append(("\u2696\ufe0f ", "Most Balanced", f"{team}\n#{hit_rank} hitting, #{pit_rank} pitching"))
    if stat_group:
        _awards(stat_group)

    # Luck Report
    _header("Luck Report")
    luck_group = []
    lucky = analysis.get("luckiest")
    if lucky:
        luck_group.append(("\U0001f340", "Luckiest Team", f"{lucky[0]}: {lucky[1]:+d} wins above expected"))
    unlucky = analysis.get("unluckiest")
    if unlucky:
        luck_group.append(("\U0001f622", "Unluckiest Team", f"{unlucky[0]}: {unlucky[1]:+d} wins below expected"))
    if luck_group:
        _awards(luck_group)

    return "\n".join(lines)


def _embed_award_fields(awards: list[tuple[str, str, str]]) -> list[dict]:
    """Return inline field pairs (label, value) for Discord embed, 2 columns per row.

    Odd-numbered last item gets inline=False so it sits alone without a spacer gap.
    For 4+ items, adds a third invisible inline field after each pair to fill
    Discord's 3-column row and prevent the next pair from merging onto it.
    """
    fields = []
    for i, (emoji, label, value) in enumerate(awards):
        is_last = i == len(awards) - 1
        is_odd_out = is_last and len(awards) % 2 == 1
        fields.append({"name": f"{emoji} {label}", "value": value, "inline": not is_odd_out})
        if (i + 1) % 2 == 0 and not is_last:
            fields.append({"name": "\u200b", "value": "\u200b", "inline": True})
    return fields


def format_discord_embeds(analysis: dict) -> list[dict]:
    """Format season report as a single Discord embed."""
    from discord_formatter import EMBED_COLOR, DIVIDER

    busts = analysis["busts"][:TOP_PICKS]

    fields = []

    # --- Final Standings ---
    fields.append({"name": DIVIDER, "value": "**\U0001f3c6 Final Standings**\n" + DIVIDER, "inline": False})
    standings_awards = []
    champion = analysis.get("champion")
    if champion:
        standings_awards.append(("\U0001f3c6", "Champion", f"{champion[0]}\n{champion[1]} playoffs"))
    rsw = analysis.get("reg_season_winner")
    if rsw:
        standings_awards.append(("\U0001f451", "Regular Season Winner", f"{rsw[0]}\n{rsw[1]}"))
    climber = analysis.get("biggest_climber")
    if climber:
        standings_awards.append(("\U0001f4c8", "Biggest Climber", f"{climber[0]}\n+{climber[1]} spots (Week {climber[2]}-{climber[3]})"))
    faller = analysis.get("biggest_faller")
    if faller:
        standings_awards.append(("\U0001f4c9", "Biggest Faller", f"{faller[0]}\n{faller[1]} spots (Week {faller[2]}-{faller[3]})"))
    if standings_awards:
        fields.extend(_embed_award_fields(standings_awards))

    # --- Season Streaks & Blowouts ---
    fields.append({"name": DIVIDER, "value": "**\U0001f525 Season Streaks & Blowouts**\n" + DIVIDER, "inline": False})
    ws = analysis.get("longest_win_streak")
    ls = analysis.get("longest_lose_streak")
    streak_awards = []
    if ws:
        streak_awards.append(("\U0001f525", "Longest Win Streak", f"{ws[0]}\n{ws[1]}W"))
    if ls:
        streak_awards.append(("\U0001f4c9", "Longest Losing Streak", f"{ls[0]}\n{ls[1]}L"))
    if streak_awards:
        fields.extend(_embed_award_fields(streak_awards))
    blowout = analysis.get("biggest_blowout")
    if blowout:
        fields.append({
            "name": "\U0001f480 Biggest Blowout",
            "value": f"{blowout['winner']} over {blowout['loser']}\n{blowout['record']} (Week {blowout['period']})",
            "inline": False,
        })

    # --- Draft Picks ---
    steals = analysis.get("steals", [])
    if steals:
        lines = []
        for i, s in enumerate(steals, 1):
            lines.append(f"**{i}.** {s['player_name']} ({s['team_name']}) - "
                         f"Pick #{s['overall_pick']} > #{s['rank']}")
        fields.append({
            "name": DIVIDER + f"\n\U0001f48e Top {TOP_PICKS} Best Draft Picks",
            "value": DIVIDER + "\n" + "\n".join(lines),
            "inline": False,
        })

    if busts:
        lines = []
        for i, b in enumerate(busts, 1):
            lines.append(f"**{i}.** {b['player_name']} ({b['team_name']}) - "
                         f"Pick #{b['overall_pick']} > #{b['rank']}")
        fields.append({
            "name": DIVIDER + f"\n\U0001f4c9 Top {TOP_PICKS} Worst Draft Picks",
            "value": DIVIDER + "\n" + "\n".join(lines),
            "inline": False,
        })

    # --- Draft Awards ---
    fields.append({"name": DIVIDER, "value": "**\U0001f3c6 Draft Awards**\n" + DIVIDER, "inline": False})
    best = analysis["best_draft"]
    if best:
        fields.append({"name": "\U0001f4c8 Best Overall Draft", "value": f"{best[0]}\n{best[1]:.1f} total score", "inline": True})
    worst_draft = analysis["worst_draft"]
    if worst_draft:
        fields.append({"name": "\U0001f4c9 Worst Overall Draft", "value": f"{worst_draft[0]}\n{worst_draft[1]:.1f} total score", "inline": True})

    # --- Waiver Wire ---
    fields.append({"name": DIVIDER, "value": "**\U0001f4b8 Waiver Wire**\n" + DIVIDER, "inline": False})
    waiver_awards = []
    bw = analysis.get("best_waiver")
    if bw:
        waiver_awards.append(("\U0001f48e", "Best Pickup", f"{bw['team']} - {bw['name']} (ranked #{bw['rank']})"))
    wm = analysis.get("most_waiver_moves")
    if wm:
        waiver_awards.append(("\U0001f504", "Most Pickups", f"{wm[0]} - {wm[1]} adds"))
    if waiver_awards:
        fields.extend(_embed_award_fields(waiver_awards))

    # --- Season Stats ---
    fields.append({"name": DIVIDER, "value": "**\u26be Season Stats**\n" + DIVIDER, "inline": False})
    stat_awards = []
    top_offense = analysis.get("top_offense")
    if top_offense:
        stat_awards.append(("\U0001f4aa", "Top Offense", f"{top_offense[0]}\n{top_offense[1]:g} pts"))
    top_pitching = analysis.get("top_pitching")
    if top_pitching:
        stat_awards.append(("\u26be", "Top Pitching", f"{top_pitching[0]}\n{top_pitching[1]:g} pts"))
    balanced = analysis.get("most_balanced")
    if balanced:
        team, hit_rank, pit_rank = balanced
        stat_awards.append(("\u2696\ufe0f", "Most Balanced", f"{team}\n#{hit_rank} hitting, #{pit_rank} pitching"))
    if stat_awards:
        fields.extend(_embed_award_fields(stat_awards))

    # --- Luck Report ---
    fields.append({"name": DIVIDER, "value": "**\U0001f340 Luck Report**\n" + DIVIDER, "inline": False})
    luck_awards = []
    lucky = analysis.get("luckiest")
    if lucky:
        luck_awards.append(("\U0001f340", "Luckiest Team", f"{lucky[0]}: {lucky[1]:+d} wins above expected"))
    unlucky = analysis.get("unluckiest")
    if unlucky:
        luck_awards.append(("\U0001f622", "Unluckiest Team", f"{unlucky[0]}: {unlucky[1]:+d} wins below expected"))
    if luck_awards:
        fields.extend(_embed_award_fields(luck_awards))

    embed = {
        "color": EMBED_COLOR,
        "title": "Season Report",
        "description": "Final standings, awards, and season stats.",
        "fields": fields,
    }
    return [embed]


def main():
    parser = argparse.ArgumentParser(description="Season Report")
    parser.add_argument("--discord", action="store_true", help="Post as Discord embed via webhook")
    parser.add_argument("--dry-run", action="store_true", help="Preview embed JSON without posting")
    parser.add_argument("--markdown", action="store_true", help="Output Discord markdown (copy/paste ready)")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL"),
                        help="Discord webhook URL")
    args = parser.parse_args()

    client = FantraxClient(LEAGUE_ID)
    print("Fetching data...", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_draft = pool.submit(client.draft_results)
        fut_scores = pool.submit(fetch_all_player_scores, client)
        fut_season = pool.submit(fetch_season_stats, client)
        fut_schedule = pool.submit(fetch_schedule_data, client)
        fut_txn_stats = pool.submit(fetch_transaction_stats, client)
        fut_rosters = pool.submit(fetch_end_of_season_rosters, client)

        draft_picks = fut_draft.result()
        for p in draft_picks:
            p["team_name"] = SHORT_NAMES.get(p["team_name"], p["team_name"])
        scores = fut_scores.result()
        season_stats = fut_season.result()
        schedule_data = fut_schedule.result()
        txn_stats = fut_txn_stats.result()
        end_rosters = fut_rosters.result()
    end_rosters = {k: SHORT_NAMES.get(v, v) for k, v in end_rosters.items()}

    num_teams = len(set(p["team_name"] for p in draft_picks))
    print(f"Found {len(draft_picks)} draft picks across {num_teams} teams, "
          f"{len(scores)} player scores, {len(end_rosters)} rostered players", file=sys.stderr)

    analysis = analyze_draft(draft_picks, scores, end_rosters)
    analysis.update(season_stats)
    analysis.update(schedule_data)
    analysis.update(txn_stats)
    print(f"Identified {len(analysis['busts'])} busts", file=sys.stderr)

    if args.discord:
        report = _build_report(analysis, markdown=True)
        if args.dry_run:
            print("\n--- DRY RUN: Discord message ---\n")
            print(report)
        else:
            if not args.webhook_url:
                print("Error: --webhook-url or DISCORD_WEBHOOK_URL required", file=sys.stderr)
                sys.exit(1)
            import requests
            resp = requests.post(args.webhook_url, json={"content": report})
            resp.raise_for_status()
            print("Posted to Discord!")
    elif args.markdown:
        print(_build_report(analysis, markdown=True))
    else:
        print(_build_report(analysis, markdown=False))


if __name__ == "__main__":
    main()
