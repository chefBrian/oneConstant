# oneConstant

Fantasy baseball Discord bot that pulls H2H Categories league data from [Fantrax](https://www.fantrax.com), computes weekly stats, and posts recaps to Discord via webhook.

## Features

- **Standings** with movement arrows (up/down from previous week)
- **Biggest winner & loser** by category differential
- **Win/loss streaks** across scoring periods
- **All-play records** - how each team would fare against every other team each week
- **Luck ratings** - compares actual record to expected all-play record
- **Category sweeps** - flags teams winning 80%+ of categories in a matchup
- **Transaction tracking** - counts adds/drops per team during each scoring period
<table>
  <tr>
    <th>Weekly Recap</th>
    <th>Transactions</th>
  </tr>
  <tr>
    <td valign="top"><img width="362" alt="Weekly Recap" src="https://github.com/user-attachments/assets/da9db2fd-0181-4cfb-ac2b-1f577b3ddd89" /></td>
    <td valign="top"><img width="410" alt="Transactions" src="https://github.com/user-attachments/assets/b82852c6-c889-46cf-bc6d-509d8af083f8" /></td>
  </tr>
</table>

## Setup


**Python 3.13+** required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `env-example.txt` to `.env` and fill in your values:

```
FANTRAX_LEAGUE_ID=your_league_id
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_TRANSACTION_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Your Fantrax league ID is in the URL when viewing your league on fantrax.com.

## Usage

```bash
# Preview embeds without posting to Discord
python bot.py --dry-run

# Preview a specific scoring period
python bot.py --dry-run --period 10

# Post the latest completed period to Discord
python bot.py

# Post a specific period
python bot.py --period 10
```

The `--dry-run` flag prints the Discord embed JSON to stdout and doesn't require a webhook URL.

### Transaction Watcher

Checks Fantrax for new transactions (adds, drops, trades, waiver claims) and posts them to Discord. In production this runs as a Cloud Function triggered by Cloud Scheduler.

```bash
# Check for new transactions and post to Discord
python transaction_watcher.py

# Preview without posting
python transaction_watcher.py --dry-run

# Post the most recent transaction and exit
python transaction_watcher.py --test
```

State is persisted in Firestore so previously posted transactions aren't re-sent. On first run, existing transactions are seeded automatically.

## Architecture

```
bot.py              → FantraxClient → compute_weekly_stats() → format_weekly_recap() → Discord webhook
transaction_watcher → FantraxClient → format_transaction_embed() / format_trade_embed() → Discord webhook
```

| File | Purpose |
|------|---------|
| `bot.py` | CLI entrypoint, Discord webhook posting |
| `fantrax_client.py` | Fantrax API client (standings, schedule, transactions, trades) |
| `stats.py` | Stat computations (all-play, luck, streaks, category kings, etc.) |
| `discord_formatter.py` | Formats stats into Discord embed payloads |
| `transaction_watcher.py` | Polls Fantrax for new transactions/trades, posts to Discord |
| `firestore_client.py` | Firestore state management for seen transaction IDs |
| `main.py` | Cloud Functions HTTP entry points (triggered by Cloud Scheduler) |

## Cloud Functions

Both the weekly recap and transaction watcher run as Google Cloud Functions (2nd gen), triggered by Cloud Scheduler.

| Function | Purpose |
|----------|---------|
| `watch_transactions` | Polls for new transactions on a schedule |
| `weekly_recap` | Posts the weekly recap (accepts optional `?period=N` query param) |

Endpoints are protected by a shared secret passed via the `X-Scheduler-Secret` header.

Required env vars for deployment (set via `gcloud`):
- `FANTRAX_LEAGUE_ID`
- `DISCORD_WEBHOOK_URL` / `DISCORD_TRANSACTION_WEBHOOK_URL`
- `SCHEDULER_SECRET`
- Firebase auth (see `env-example.txt` for options)

## Notes

- Built for **H2H Categories** leagues only (not rotisserie or points).
- The Fantrax API is undocumented and reverse-engineered - response shapes may change without notice.
- "Lower is better" categories (ERA, WHIP, BB/9, L, HRA) are hardcoded in `stats.py`. Update `LOWER_IS_BETTER` if your league categories differ.
- The "latest completed period" is detected by checking which periods have non-zero matchup scores, not by date.
