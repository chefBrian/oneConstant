"""Transaction watcher - checks Fantrax for new transactions and posts to Discord.

Usage:
    python transaction_watcher.py              # Check once and exit
    python transaction_watcher.py --dry-run    # Preview without posting
    python transaction_watcher.py --test       # Post most recent transaction only
"""
import argparse
import json
import os
import sys

import requests

from fantrax_client import FantraxClient
from discord_formatter import format_transaction_embed, format_trade_embed
from firestore_client import (
    has_been_seeded,
    load_seen_ids,
    save_seen_ids,
    seed_seen_ids,
)


def send_embed(webhook_url: str, embed: dict) -> bool:
    """Send a single embed to Discord. Returns True on success."""
    resp = requests.post(webhook_url, json={"embeds": [embed]})
    if resp.status_code == 204:
        print(f"  Posted to Discord")
        return True
    else:
        print(f"  Discord error {resp.status_code}: {resp.text}", file=sys.stderr)
        return False


def fetch_all_tx_ids(client: FantraxClient) -> tuple[list[dict], list[dict]]:
    """Fetch current transactions and trades."""
    txns = client.transactions(count=50)
    trades = client.trades(count=20)
    return txns, trades


def check_once(league_id: str, webhook_url: str | None, dry_run: bool) -> None:
    """Single check cycle: fetch transactions, post new ones, update Firestore.

    This is the core logic called by both the CLI and the Cloud Functions
    entry point (main.py).
    """
    client = FantraxClient(league_id)
    txns, trades = fetch_all_tx_ids(client)

    # First-run detection: seed Firestore with all current IDs
    if not has_been_seeded(league_id):
        all_ids = [t["tx_set_id"] for t in txns] + [t["tx_set_id"] for t in trades]
        seed_seen_ids(league_id, all_ids)
        print(f"Seeded Firestore with {len(all_ids)} existing transactions")
        return

    # Check which IDs from the current batch are already seen
    all_current_ids = [t["tx_set_id"] for t in txns] + [t["tx_set_id"] for t in trades]
    seen_ids = load_seen_ids(league_id, all_current_ids)

    new_txns = [t for t in txns if t["tx_set_id"] not in seen_ids]
    new_trades = [t for t in trades if t["tx_set_id"] not in seen_ids]

    if not new_txns and not new_trades:
        return

    # Post newest last (reverse since API returns newest first)
    # Only save IDs for transactions that were successfully posted
    successfully_posted = []

    for txn in reversed(new_txns):
        embed = format_transaction_embed(txn)
        print(f"  NEW: {txn['team_name']} ({txn['type']})")

        if dry_run:
            print(json.dumps(embed, indent=2, ensure_ascii=False))
            print()
            successfully_posted.append(txn["tx_set_id"])
        elif webhook_url:
            if send_embed(webhook_url, embed):
                successfully_posted.append(txn["tx_set_id"])

    for trade in reversed(new_trades):
        embed = format_trade_embed(trade)
        player_names = [p["name"] for p in trade["players"]]
        print(f"  NEW TRADE: {', '.join(player_names[:4])}...")

        if dry_run:
            print(json.dumps(embed, indent=2, ensure_ascii=False))
            print()
            successfully_posted.append(trade["tx_set_id"])
        elif webhook_url:
            if send_embed(webhook_url, embed):
                successfully_posted.append(trade["tx_set_id"])

    save_seen_ids(league_id, successfully_posted)


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Watch Fantrax for new transactions")
    parser.add_argument("--league-id", default=os.environ.get("FANTRAX_LEAGUE_ID"))
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_TRANSACTION_WEBHOOK_URL"))
    parser.add_argument("--dry-run", action="store_true", help="Print embeds instead of posting")
    parser.add_argument("--test", action="store_true",
                        help="Post the most recent transaction and exit")
    args = parser.parse_args()

    if not args.league_id:
        print("Error: --league-id or FANTRAX_LEAGUE_ID required", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and not args.test and not args.webhook_url:
        print("Error: --webhook-url or DISCORD_TRANSACTION_WEBHOOK_URL required", file=sys.stderr)
        sys.exit(1)

    # --test mode: post the most recent transaction and exit
    if args.test:
        client = FantraxClient(args.league_id)
        txns = client.transactions(count=5)
        if txns:
            embed = format_transaction_embed(txns[0])
            print("Most recent transaction:")
            print(json.dumps(embed, indent=2, ensure_ascii=False))
            if not args.dry_run and args.webhook_url:
                send_embed(args.webhook_url, embed)
        else:
            print("No transactions found")
        return

    check_once(args.league_id, args.webhook_url, args.dry_run)


if __name__ == "__main__":
    main()
