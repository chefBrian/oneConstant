"""Transaction watcher — polls Fantrax and posts new transactions to Discord.

Usage:
    python transaction_watcher.py                    # Run with env vars
    python transaction_watcher.py --dry-run           # Preview without posting
    python transaction_watcher.py --interval 60       # Poll every 60 seconds
    python transaction_watcher.py --test              # Post most recent transaction only
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import requests

load_dotenv()

from fantrax_client import FantraxClient
from discord_formatter import format_transaction_embed, format_trade_embed

STATE_FILE = Path(__file__).parent / "seen_transactions.json"


def load_state() -> set[str]:
    """Load seen transaction IDs from state file."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("seen_ids", []))
    return set()


def save_state(seen_ids: set[str]) -> None:
    """Save seen transaction IDs to state file."""
    STATE_FILE.write_text(json.dumps({"seen_ids": sorted(seen_ids)}, indent=2))


def send_embed(webhook_url: str, embed: dict) -> None:
    """Send a single embed to Discord."""
    resp = requests.post(webhook_url, json={"embeds": [embed]})
    if resp.status_code == 204:
        print(f"  Posted to Discord")
    else:
        print(f"  Discord error {resp.status_code}: {resp.text}", file=sys.stderr)


def fetch_all_tx_ids(client: FantraxClient) -> tuple[list[dict], list[dict]]:
    """Fetch current transactions and trades."""
    txns = client.transactions(count=50)
    trades = client.trades(count=20)
    return txns, trades


def check_for_new(client: FantraxClient, seen_ids: set[str],
                   webhook_url: str | None, dry_run: bool) -> set[str]:
    """Check for new transactions, post them, return updated seen_ids."""
    txns, trades = fetch_all_tx_ids(client)

    new_txns = [t for t in txns if t["tx_set_id"] not in seen_ids]
    new_trades = [t for t in trades if t["tx_set_id"] not in seen_ids]

    if not new_txns and not new_trades:
        return seen_ids

    # Post newest last (reverse since API returns newest first)
    for txn in reversed(new_txns):
        embed = format_transaction_embed(txn)
        print(f"  NEW: {txn['team_name']} ({txn['type']})")

        if dry_run:
            print(json.dumps(embed, indent=2, ensure_ascii=False))
            print()
        elif webhook_url:
            send_embed(webhook_url, embed)

        seen_ids.add(txn["tx_set_id"])

    for trade in reversed(new_trades):
        embed = format_trade_embed(trade)
        player_names = [p["name"] for p in trade["players"]]
        print(f"  NEW TRADE: {', '.join(player_names[:4])}...")

        if dry_run:
            print(json.dumps(embed, indent=2, ensure_ascii=False))
            print()
        elif webhook_url:
            send_embed(webhook_url, embed)

        seen_ids.add(trade["tx_set_id"])

    return seen_ids


def seed_state(client: FantraxClient) -> set[str]:
    """Seed state with all current transactions so we don't spam on first run."""
    txns, trades = fetch_all_tx_ids(client)
    ids = {t["tx_set_id"] for t in txns}
    ids.update(t["tx_set_id"] for t in trades)
    print(f"Seeded state with {len(ids)} existing transactions")
    return ids


def main():
    parser = argparse.ArgumentParser(description="Watch Fantrax for new transactions")
    parser.add_argument("--league-id", default=os.environ.get("FANTRAX_LEAGUE_ID"))
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_TRANSACTION_WEBHOOK_URL"))
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds")
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

    client = FantraxClient(args.league_id)

    # --test mode: post the most recent transaction and exit
    if args.test:
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

    # Load or seed state
    seen_ids = load_state()
    if not seen_ids:
        seen_ids = seed_state(client)
        save_state(seen_ids)

    print(f"Watching for transactions (polling every {args.interval}s)...")
    if args.dry_run:
        print("DRY RUN mode — embeds will be printed, not posted")

    try:
        while True:
            try:
                seen_ids = check_for_new(client, seen_ids, args.webhook_url, args.dry_run)
                save_state(seen_ids)
            except requests.RequestException as e:
                print(f"  Network error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"  Error: {e}", file=sys.stderr)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
