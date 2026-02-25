"""2025 Season Report: draft busts, awards, and season stats.

Pulls 2025 draft results, player scores, transactions, and schedule data
to generate a full season summary.

Usage:
    python draft_roast.py                    # CLI output
    python draft_roast.py --discord          # Post to Discord
    python draft_roast.py --dry-run          # Preview Discord embed JSON
"""
import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()

from fantrax_client import FantraxClient
from stats import LOWER_IS_BETTER

LEAGUE_2025 = os.environ.get("FANTRAX_LEAGUE_ID", "uo0es7lom23shg6b")

# Only consider picks from rounds 1 through this
MAX_ROUND = 10

# Minimum bust score (actual_rank - draft_pick) to count as a bust
MIN_BUST_SCORE = 150

# Number of busts to show
TOP_BUSTS = 5

# Shorten long team names for mobile formatting
SHORT_NAMES = {
    "Friedl Dee & Friedl Dum": "Friedl Dee",
}


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


def fetch_schedule_stats(client: FantraxClient) -> dict:
    """Fetch luck stats from full regular season schedule (first 20 periods)."""
    schedule = client.schedule()

    # First 20 periods are regular season; periods 21-23 are playoffs (numbered 1-3 again)
    reg_season = [p for p in schedule[:20] if p["matchups"]]
    if not reg_season:
        return {}

    last_period = reg_season[-1]["period_num"]

    # Luck: all-play record vs actual record
    from stats import _all_play_record
    standings = client.standings(period=last_period)
    all_play = _all_play_record(reg_season, last_period)
    num_opponents = max(1, len(standings) - 1)

    luck = {}
    for s in standings:
        name = s["team_name"]
        if name in all_play:
            ap = all_play[name]
            disp_ap_w = round(ap["wins"] / num_opponents)
            games_back = s["wins"] - disp_ap_w
            luck[name] = games_back

    sorted_luck = sorted(luck.items(), key=lambda x: x[1], reverse=True)
    luckiest = sorted_luck[0] if sorted_luck else None
    unluckiest = sorted_luck[-1] if sorted_luck else None

    return {
        "luckiest": luckiest,
        "unluckiest": unluckiest,
    }


def fetch_end_of_season_rosters(client: FantraxClient) -> dict[str, str]:
    """Fetch rosters at end of regular season (period 20). Returns {player_name: team_name}."""
    data = client._call("getTeamRosterInfo", period="20")
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
        d = c._call("getTeamRosterInfo", teamId=tid, period="20")
        return _parse_roster(d, tid)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for roster in pool.map(_fetch_team, remaining):
            all_rosters.update(roster)

    return all_rosters


def fetch_transaction_stats(client: FantraxClient) -> dict:
    """Fetch transaction counts per team (adds only, not drops)."""
    txns = client.transactions(count=500)
    counts = Counter(t["team_name"] for t in txns if t.get("added"))
    most = counts.most_common(1)[0] if counts else None
    return {"most_waiver_moves": most}


def analyze_draft(draft_picks: list[dict], scores: dict[str, dict], end_rosters: dict[str, str]) -> dict:
    """Analyze draft picks and find busts, values, and waiver gems."""
    busts = []
    values = []
    team_totals: dict[str, float] = {}
    drafted_sids = {p["scorer_id"] for p in draft_picks}

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

        # Value picks: finished way above draft position (negative bust score = good)
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
    top_busts = busts[:TOP_BUSTS]
    worst = top_busts[0] if top_busts else None

    early_busts = [b for b in busts if b["round"] <= 3]
    biggest_reach = max(early_busts, key=lambda x: x["bust_score"]) if early_busts else None

    sorted_teams = sorted(team_totals.items(), key=lambda x: x[1], reverse=True)
    best_draft = sorted_teams[0] if sorted_teams else None
    worst_draft = sorted_teams[-1] if sorted_teams else None

    # Top steals: picks that outperformed their draft position the most
    steals = [v for v in values if v["bust_score"] < 0][:TOP_BUSTS]

    return {
        "busts": busts,
        "steals": steals,
        "team_bust_counts": team_counts,
        "worst_pick": worst,
        "biggest_reach": biggest_reach,
        "best_draft": best_draft,
        "worst_draft": worst_draft,
        "best_value": best_value,
        "most_predictable": predictable,
        "best_waiver": best_waiver,
    }


def _build_report(analysis: dict, markdown: bool = False) -> str:
    """Build season report. If markdown=True, includes Discord formatting."""
    busts = analysis["busts"][:TOP_BUSTS]
    steals = analysis.get("steals", [])
    b = "**" if markdown else ""  # bold wrapper
    lines = []

    def _pick_table(picks):
        tw = max(4, max(len(p["team_name"]) for p in picks))
        pw = max(6, max(len(p["player_name"]) for p in picks))
        tbl = []
        tbl.append(f"{'#':<3} {'Team':<{tw}}  {'Player':<{pw}}  {'Pick':>5}  {'Rank':>5}")
        tbl.append("-" * (3 + 2 + tw + 2 + pw + 2 + 5 + 2 + 5))
        for i, p in enumerate(picks, 1):
            tbl.append(f"{i:<3} {p['team_name']:<{tw}}  {p['player_name']:<{pw}}  "
                       f"#{p['overall_pick']:>4}  #{p['rank']:>4}")
        if markdown:
            return "```\n" + "\n".join(tbl) + "\n```"
        return "\n".join(tbl)

    def _header(title):
        if markdown:
            lines.append(f"\n## {title}")
        else:
            lines.append("")
            lines.append("=" * 60)
            lines.append(f"  {title}")
            lines.append("=" * 60)

    def _awards(awards: list[tuple[str, str, str]]):
        """Append a group of awards with team name and clarifying stat on separate lines."""
        max_label = max(len(label) for _, label, _ in awards)
        for emoji, label, value in awards:
            padded = f"{label}:".ljust(max_label + 1)
            team, _, stat = value.partition("\n")
            lines.append(f"{emoji} {b}{padded}{b} {team}")
            if stat:
                lines.append(f"{'':>{max_label + 5}}{stat}")

    # Title
    if markdown:
        lines.append("# \U0001f3c6 2025 Season Report")
    else:
        lines.append("\U0001f3c6 2025 Season Report")

    # Best picks
    if steals:
        _header("\U0001f48e Best Draft Picks")
        lines.append(_pick_table(steals))

    # Worst picks
    if busts:
        _header("\U0001f4c9 Worst Draft Picks")
        lines.append(_pick_table(busts))

    # Draft Awards
    _header("\U0001f3c6 Draft Awards")
    draft_group = []
    best = analysis["best_draft"]
    if best:
        draft_group.append(("\U0001f4c8", "Best Overall Draft", f"{best[0]}\n{best[1]:.1f} total score"))
    worst_draft = analysis["worst_draft"]
    if worst_draft:
        draft_group.append(("\U0001f4c9", "Worst Overall Draft", f"{worst_draft[0]}\n{worst_draft[1]:.1f} total score"))
    counts = analysis["team_bust_counts"]
    if counts:
        shame_team, shame_count = counts.most_common(1)[0]
        draft_group.append(("\U0001f921", "Most Busts Drafted", f"{shame_team}\n{shame_count} busts"))
    if draft_group:
        _awards(draft_group)

    # Waiver Wire
    _header("\U0001f4b8 Waiver Wire")
    waiver_group = []
    bw = analysis.get("best_waiver")
    if bw:
        waiver_group.append(("\U0001f48e", "Best Pickup", f"{bw['team']}\n{bw['name']} (ranked #{bw['rank']})"))
    wm = analysis.get("most_waiver_moves")
    if wm:
        waiver_group.append(("\U0001f504", "Most Pickups", f"{wm[0]}\n{wm[1]} adds"))
    if waiver_group:
        _awards(waiver_group)

    # Season Stats
    _header("\u26be Season Stats")
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
    _header("\U0001f340 Luck Report")
    luck_group = []
    lucky = analysis.get("luckiest")
    if lucky:
        luck_group.append(("\U0001f340", "Luckiest Team", f"{lucky[0]}\n{lucky[1]:+d} wins above all-play"))
    unlucky = analysis.get("unluckiest")
    if unlucky:
        luck_group.append(("\U0001f622", "Unluckiest Team", f"{unlucky[0]}\n{unlucky[1]:+d} wins below all-play"))
    if luck_group:
        _awards(luck_group)

    return "\n".join(lines)


def _embed_award_fields(awards: list[tuple[str, str, str]]) -> list[dict]:
    """Return inline field pairs (label, value) for Discord embed, 2 columns per row.

    Odd-numbered last item gets inline=False so it sits alone without a spacer gap.
    """
    fields = []
    for i, (emoji, label, value) in enumerate(awards):
        is_last = i == len(awards) - 1
        is_odd_out = is_last and len(awards) % 2 == 1
        fields.append({"name": f"{emoji} {label}", "value": value, "inline": not is_odd_out})
    return fields


def format_discord_embeds(analysis: dict) -> list[dict]:
    """Format season report as Discord embeds."""
    from discord_formatter import EMBED_COLOR, DIVIDER

    busts = analysis["busts"][:TOP_BUSTS]

    fields = []

    # Best picks table
    steals = analysis.get("steals", [])
    if steals:
        lines = []
        for i, s in enumerate(steals, 1):
            lines.append(f"**{i}.** {s['player_name']} ({s['team_name']}) - "
                         f"Pick #{s['overall_pick']} > #{s['rank']}")
        fields.append({
            "name": DIVIDER + f"\n\U0001f48e Top {TOP_BUSTS} Best Draft Picks",
            "value": DIVIDER + "\n" + "\n".join(lines),
            "inline": False,
        })

    # Bust table - single line per pick
    if busts:
        lines = []
        for i, b in enumerate(busts, 1):
            lines.append(f"**{i}.** {b['player_name']} ({b['team_name']}) - "
                         f"Pick #{b['overall_pick']} > #{b['rank']}")
        fields.append({
            "name": DIVIDER + f"\n\U0001f4c9 Top {TOP_BUSTS} Worst Draft Picks",
            "value": DIVIDER + "\n" + "\n".join(lines),
            "inline": False,
        })

    # Draft Awards
    fields.append({"name": DIVIDER, "value": "**\U0001f3c6 Draft Awards**\n" + DIVIDER, "inline": False})
    best = analysis["best_draft"]
    if best:
        fields.append({"name": "\U0001f4c8 Best Overall Draft", "value": f"{best[0]}\n{best[1]:.1f} total score", "inline": True})
    worst_draft = analysis["worst_draft"]
    if worst_draft:
        fields.append({"name": "\U0001f4c9 Worst Overall Draft", "value": f"{worst_draft[0]}\n{worst_draft[1]:.1f} total score", "inline": True})
    counts = analysis["team_bust_counts"]
    if counts:
        shame_team, shame_count = counts.most_common(1)[0]
        fields.append({"name": "\U0001f921 Most Busts Drafted", "value": f"{shame_team}\n{shame_count} busts", "inline": False})

    # Waiver Wire
    fields.append({"name": DIVIDER, "value": "**\U0001f4b8 Waiver Wire**\n" + DIVIDER, "inline": False})
    waiver_awards = []
    bw = analysis.get("best_waiver")
    if bw:
        waiver_awards.append(("\U0001f48e", "Best Pickup", f"{bw['team']}\n{bw['name']} (ranked #{bw['rank']})"))
    wm = analysis.get("most_waiver_moves")
    if wm:
        waiver_awards.append(("\U0001f504", "Most Pickups", f"{wm[0]}\n{wm[1]} adds"))
    if waiver_awards:
        fields.extend(_embed_award_fields(waiver_awards))

    # Season Stats
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

    # Luck Report
    fields.append({"name": DIVIDER, "value": "**\U0001f340 Luck Report**\n" + DIVIDER, "inline": False})
    luck_awards = []
    lucky = analysis.get("luckiest")
    if lucky:
        luck_awards.append(("\U0001f340", "Luckiest Team", f"{lucky[0]}\n{lucky[1]:+d} wins above all-play"))
    unlucky = analysis.get("unluckiest")
    if unlucky:
        luck_awards.append(("\U0001f622", "Unluckiest Team", f"{unlucky[0]}\n{unlucky[1]:+d} wins below all-play"))
    if luck_awards:
        fields.extend(_embed_award_fields(luck_awards))

    embed = {
        "color": EMBED_COLOR,
        "title": "2025 Season Report",
        "description": "Draft busts, awards, and season stats.",
        "fields": fields,
    }
    return [embed]


def main():
    parser = argparse.ArgumentParser(description="2025 Season Report")
    parser.add_argument("--discord", action="store_true", help="Post as Discord embed via webhook")
    parser.add_argument("--dry-run", action="store_true", help="Preview embed JSON without posting")
    parser.add_argument("--markdown", action="store_true", help="Output Discord markdown (copy/paste ready)")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL"),
                        help="Discord webhook URL")
    args = parser.parse_args()

    client = FantraxClient(LEAGUE_2025)
    print("Fetching data...", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_draft = pool.submit(client.draft_results)
        fut_scores = pool.submit(fetch_all_player_scores, client)
        fut_season = pool.submit(fetch_season_stats, client)
        fut_schedule = pool.submit(fetch_schedule_stats, client)
        fut_txn_stats = pool.submit(fetch_transaction_stats, client)
        fut_rosters = pool.submit(fetch_end_of_season_rosters, client)

        draft_picks = fut_draft.result()
        for p in draft_picks:
            p["team_name"] = SHORT_NAMES.get(p["team_name"], p["team_name"])
        scores = fut_scores.result()
        season_stats = fut_season.result()
        schedule_stats = fut_schedule.result()
        txn_stats = fut_txn_stats.result()
        end_rosters = fut_rosters.result()
    end_rosters = {k: SHORT_NAMES.get(v, v) for k, v in end_rosters.items()}

    num_teams = len(set(p["team_name"] for p in draft_picks))
    print(f"Found {len(draft_picks)} draft picks across {num_teams} teams, "
          f"{len(scores)} player scores, {len(end_rosters)} rostered players", file=sys.stderr)

    analysis = analyze_draft(draft_picks, scores, end_rosters)
    analysis.update(season_stats)
    analysis.update(schedule_stats)
    analysis.update(txn_stats)
    print(f"Identified {len(analysis['busts'])} busts", file=sys.stderr)

    if args.discord:
        embeds = format_discord_embeds(analysis)
        if args.dry_run:
            print("\n--- DRY RUN: Discord embed ---\n")
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        else:
            if not args.webhook_url:
                print("Error: --webhook-url or DISCORD_WEBHOOK_URL required", file=sys.stderr)
                sys.exit(1)
            from bot import send_to_discord
            send_to_discord(args.webhook_url, embeds)
            print("Posted to Discord!")
    elif args.markdown:
        report = _build_report(analysis, markdown=False)
        print("```")
        print(report)
        print("```")
    else:
        print(_build_report(analysis, markdown=False))


if __name__ == "__main__":
    main()
