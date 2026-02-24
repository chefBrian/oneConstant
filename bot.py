"""oneConstant - Weekly Fantrax Fantasy Baseball Discord Bot.

Pulls league data from Fantrax, computes fun stats, and posts to Discord.

Usage:
    python bot.py                    # Auto-detect latest completed period
    python bot.py --period 10        # Specific period
    python bot.py --dry-run          # Print embeds without posting
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv
import requests

load_dotenv()

from fantrax_client import FantraxClient
from stats import compute_weekly_stats
from discord_formatter import format_weekly_recap


def send_to_discord(webhook_url: str, embeds: list[dict]) -> None:
    """Send embeds to Discord via webhook.

    Discord allows max 10 embeds per message. We batch if needed.
    """
    batch_size = 10
    for i in range(0, len(embeds), batch_size):
        batch = embeds[i:i + batch_size]
        payload = {"embeds": batch}
        resp = requests.post(webhook_url, json=payload)
        if resp.status_code == 204:
            print(f"Posted {len(batch)} embeds to Discord")
        else:
            print(f"Discord error {resp.status_code}: {resp.text}", file=sys.stderr)
            resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description="oneConstant - Fantrax to Discord weekly recap")
    parser.add_argument("--league-id", default=os.environ.get("FANTRAX_LEAGUE_ID"),
                        help="Fantrax league ID")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL"),
                        help="Discord webhook URL")
    parser.add_argument("--period", type=int, default=None,
                        help="Specific scoring period number (default: latest completed)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print embeds to stdout instead of posting")
    args = parser.parse_args()

    if not args.league_id:
        print("Error: --league-id or FANTRAX_LEAGUE_ID env var required", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and not args.webhook_url:
        print("Error: --webhook-url or DISCORD_WEBHOOK_URL env var required (unless --dry-run)", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching data for league {args.league_id}...")
    client = FantraxClient(args.league_id)
    print(f"League: {client.team_map and 'loaded'}")

    print("Computing stats...")
    stats = compute_weekly_stats(client, period_num=args.period)

    if "error" in stats:
        print(f"Error: {stats['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Period: {stats['period']['name']} {stats['period']['date_range']}")

    embeds = format_weekly_recap(stats, league_id=args.league_id)

    if args.dry_run:
        print("\n--- DRY RUN: Embeds that would be posted ---\n")
        print(json.dumps(embeds, indent=2, ensure_ascii=False))
    else:
        send_to_discord(args.webhook_url, embeds)
        print("Done!")


if __name__ == "__main__":
    main()
