"""Power Rankings: sum of Fantrax category points (12 = best, 1 = worst per cat).

Pulls SEASON_STATS view and ranks teams by hitting points + pitching points.
Combined ranking matches the league standings; the value-add here is showing
the hitting vs pitching breakdown side by side.

Usage:
    python reports/power_rankings.py              # CLI table
    python reports/power_rankings.py --dry-run    # Preview Discord embed JSON
    python reports/power_rankings.py --post       # Post to Discord
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

load_dotenv()

from clients.fantrax_client import FantraxClient
from utils.discord_formatter import COLOR_BLUE, DIVIDER

LEAGUE_ID = os.environ.get("FANTRAX_LEAGUE_ID", "uo0es7lom23shg6b")

RANK_EMOJI = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}

# Post on the Monday after weeks 4, 8, 12, 16 complete.
# Week 20 is skipped since the season finale report covers end-of-season.
POST_PERIODS = {4, 8, 12, 16}
REG_SEASON_PERIODS = 20


def _fmt_points(p: float) -> str:
    """Drop trailing .0 but keep .5 for ties."""
    return f"{p:g}"


def rank_teams(teams: list[dict]) -> list[dict]:
    """Return teams sorted by total points (desc), with rank fields attached."""
    ranked = sorted(teams, key=lambda t: t["total_points"], reverse=True)
    hit_order = sorted(teams, key=lambda t: t["hitting_points"], reverse=True)
    pit_order = sorted(teams, key=lambda t: t["pitching_points"], reverse=True)
    hit_rank = {t["team_name"]: i + 1 for i, t in enumerate(hit_order)}
    pit_rank = {t["team_name"]: i + 1 for i, t in enumerate(pit_order)}
    for i, t in enumerate(ranked):
        t["overall_rank"] = i + 1
        t["hitting_rank"] = hit_rank[t["team_name"]]
        t["pitching_rank"] = pit_rank[t["team_name"]]
    return ranked


def print_cli_table(ranked: list[dict], hit_cats: list[str], pit_cats: list[str]) -> None:
    name_w = max(len(t["team_name"]) for t in ranked)
    name_w = max(name_w, len("Team"))

    print(f"Power Rankings - {len(hit_cats)} hitting cats + {len(pit_cats)} pitching cats "
          f"(max {12 * (len(hit_cats) + len(pit_cats))} pts)")
    print()
    header = f"{'#':>2}  {'Team':<{name_w}}  {'Hit':>6} {'(H#)':>5}  {'Pit':>6} {'(P#)':>5}  {'Total':>6}"
    print(header)
    print("-" * len(header))
    for t in ranked:
        hit_rank_str = f"(#{t['hitting_rank']})"
        pit_rank_str = f"(#{t['pitching_rank']})"
        print(
            f"{t['overall_rank']:>2}  {t['team_name']:<{name_w}}  "
            f"{_fmt_points(t['hitting_points']):>6} {hit_rank_str:>5}  "
            f"{_fmt_points(t['pitching_points']):>6} {pit_rank_str:>5}  "
            f"{_fmt_points(t['total_points']):>6}"
        )


def format_discord_embed(ranked: list[dict], hit_cats: list[str], pit_cats: list[str],
                          league_id: str, period_num: int | None = None) -> dict:
    name_w = max(len(t["team_name"]) for t in ranked)

    lines = [f"`{'#':>2}  {'Team':<{name_w}}  {'Hit':>5}  {'Pit':>5}  {'Tot':>5}`"]
    for t in ranked:
        rank = t["overall_rank"]
        prefix = RANK_EMOJI.get(rank, f"{rank:>2}")
        # Use code-block-style line for alignment on desktop and mobile.
        lines.append(
            f"`{rank:>2}  {t['team_name']:<{name_w}}  "
            f"{_fmt_points(t['hitting_points']):>5}  "
            f"{_fmt_points(t['pitching_points']):>5}  "
            f"{_fmt_points(t['total_points']):>5}`"
            + (f"  {prefix}" if rank <= 3 else "")
        )

    description = "\n".join(lines)

    # Highlight best in each phase
    best_hit = max(ranked, key=lambda t: t["hitting_points"])
    best_pit = max(ranked, key=lambda t: t["pitching_points"])
    fields = [
        {
            "name": "\U0001f4aa Top Offense",
            "value": f"{best_hit['team_name']}\n{_fmt_points(best_hit['hitting_points'])} pts",
            "inline": True,
        },
        {
            "name": "⚾ Top Pitching",
            "value": f"{best_pit['team_name']}\n{_fmt_points(best_pit['pitching_points'])} pts",
            "inline": True,
        },
    ]

    title = "\U0001f4ca Power Rankings"
    if period_num is not None:
        title += f" (Week {period_num}/{REG_SEASON_PERIODS})"

    embed = {
        "color": COLOR_BLUE,
        "title": title,
        "url": f"https://www.fantrax.com/fantasy/league/{league_id}/standings;view=SEASON_STATS",
        "description": description,
        "fields": fields,
    }
    return embed


def run_power_rankings(
    league_id: str,
    webhook_url: str | None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """Shared entry point for CLI and Cloud Functions.

    By default only posts when the latest completed period is in POST_PERIODS
    (weeks 4, 8, 12, 16, 20). Pass force=True to post regardless of period.
    Returns a short status string for logging.
    """
    client = FantraxClient(league_id)

    latest = client.latest_completed_period()
    period_num = latest["period_num"] if latest else None

    if not force and period_num not in POST_PERIODS:
        msg = f"Skipping: latest completed period is {period_num}, not in {sorted(POST_PERIODS)}"
        print(msg, file=sys.stderr)
        return msg

    print(f"Fetching season stats for league {league_id}...", file=sys.stderr)
    ss = client.season_stats()
    ranked = rank_teams(ss["teams"])
    embed = format_discord_embed(
        ranked, ss["hitting_categories"], ss["pitching_categories"], league_id,
        period_num=period_num,
    )

    if dry_run:
        print(json.dumps({"embeds": [embed]}, indent=2, ensure_ascii=False))
        return "Dry run"

    if not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK_URL required to post")

    resp = requests.post(webhook_url, json={"embeds": [embed]})
    if resp.status_code != 204:
        print(f"Discord error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    print("Posted power rankings to Discord")
    return "Posted"


def main():
    parser = argparse.ArgumentParser(description="Power Rankings (season category points)")
    parser.add_argument("--dry-run", action="store_true", help="Print Discord embed JSON without posting")
    parser.add_argument("--post", action="store_true", help="Post to Discord webhook")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the every-4-weeks gate (post even if not week 4/8/12/16/20)")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL"),
                        help="Discord webhook URL (defaults to DISCORD_WEBHOOK_URL env var)")
    args = parser.parse_args()

    if args.dry_run or args.post:
        run_power_rankings(LEAGUE_ID, args.webhook_url, dry_run=args.dry_run, force=args.force)
        return

    # Default CLI behavior: print the table (no period gate)
    client = FantraxClient(LEAGUE_ID)
    print(f"Fetching season stats for league {LEAGUE_ID}...", file=sys.stderr)
    ss = client.season_stats()
    ranked = rank_teams(ss["teams"])
    print_cli_table(ranked, ss["hitting_categories"], ss["pitching_categories"])


if __name__ == "__main__":
    main()
