# oneConstant

Fantasy baseball Discord bot that pulls H2H Categories league data from [Fantrax](https://www.fantrax.com), computes weekly stats, and posts recaps to Discord via webhook.

## Features

- **Standings** with movement arrows (up/down from previous week)
- **Biggest winner & loser** by category differential
- **Win/loss streaks** across scoring periods
- **All-play records** — how each team would fare against every other team each week
- **Luck ratings** — compares actual record to expected all-play record
- **Category sweeps** — flags teams winning 80%+ of categories in a matchup
- **Transaction tracking** — counts adds/drops per team during each scoring period
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

## Architecture

```
bot.py → FantraxClient → compute_weekly_stats() → format_weekly_recap() → Discord webhook
```

| File | Purpose |
|------|---------|
| `bot.py` | CLI entrypoint, Discord webhook posting |
| `fantrax_client.py` | Fantrax API client (standings, schedule, transactions, trades) |
| `stats.py` | Stat computations (all-play, luck, streaks, category kings, etc.) |
| `discord_formatter.py` | Formats stats into Discord embed payloads |

## Automated Recaps

A GitHub Actions workflow runs every Monday at 8:00 AM ET to post the weekly recap automatically. You can also trigger it manually with an optional scoring period input.

Set these repository secrets:
- `FANTRAX_LEAGUE_ID`
- `DISCORD_WEBHOOK_URL`

## Notes

- Built for **H2H Categories** leagues only (not rotisserie or points).
- The Fantrax API is undocumented and reverse-engineered — response shapes may change without notice.
- "Lower is better" categories (ERA, WHIP, BB/9, L, HRA) are hardcoded in `stats.py`. Update `LOWER_IS_BETTER` if your league categories differ.
- The "latest completed period" is detected by checking which periods have non-zero matchup scores, not by date.
