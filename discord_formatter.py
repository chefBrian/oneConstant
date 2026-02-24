"""Format weekly stats into Discord webhook embeds."""

# Discord embed color
EMBED_COLOR = 0x0099FF

# Custom server emojis
PLUS = "<:plus:826595998188175360>"
MINUS = "<:minus:826596117923889152>"
UP = "<:upchevron:1115050941834596454>"
DOWN = "<:downchevron:1115050940681158717>"
BLANK = "<:blank:1085633856868208640>"
TRADE = "<:trade:826598334521147433>"
DIVIDER = "\u2500" * 45

# Spacer fields for visual breathing room
SPACER = {"name": "\u200b", "value": DIVIDER, "inline": False}
SPACER_SM = {"name": "", "value": "\u200b", "inline": False}
SPACER_INLINE = {"name": "\u200b", "value": "\u200b", "inline": True}


FANTRAX_STATS_URL = "https://www.fantrax.com/fantasy/league/{league_id}/standings;view=SEASON_STATS"
WHITESPACE_IMG = "https://i.imgur.com/sv3vQS9.png"


def format_weekly_recap(stats: dict, league_id: str = "") -> list[dict]:
    """Build Discord webhook embeds from computed stats.

    Returns a list of embed dicts ready for the Discord webhook payload.
    """
    period = stats["period"]
    period_num = period["period_num"]
    total_periods = stats.get("total_periods", 0)
    period_name = f"Week {period_num} / {total_periods}" if total_periods else period["name"]
    date_range = period["date_range"]
    stats_url = FANTRAX_STATS_URL.format(league_id=league_id) if league_id else ""

    fields = []

    # --- Standings ---
    fields.extend(_standings_fields(stats))
    fields.append(SPACER)

    # --- Streaks ---
    fields.extend(_streaks_fields(stats))

    # --- Hot Takes ---
    fields.extend(_hot_takes_fields(stats))

    # --- All-Play & Luck ---
    fields.extend(_all_play_fields(stats))

    embed = {
        "color": EMBED_COLOR,
        "title": period_name,
        "fields": fields,
    }
    if stats_url:
        embed["url"] = stats_url

    return [embed]


def _standings_fields(stats: dict) -> list[dict]:
    standings = stats["standings"]
    movement = stats.get("standings_movement", {})

    fields = []
    for i, s in enumerate(standings):
        rank = s["rank"]
        name = s["team_name"]
        gb = s.get("games_back", "0")

        move = movement.get(name, 0)
        if move > 0:
            prefix = UP
        elif move < 0:
            prefix = DOWN
        else:
            prefix = BLANK

        gb_str = f"({gb} GB)" if gb and gb != "0" else ""

        fields.append({
            "name": "",
            "value": f"{prefix} {rank}. {name} {gb_str}",
            "inline": False,
        })

        # Separator between playoff (top 6) and non-playoff teams
        if i == 5:
            fields.append({"name": "", "value": DIVIDER, "inline": False})

    return fields


def _hot_takes_fields(stats: dict) -> list[dict]:
    fields = []

    bb = stats.get("biggest_blowout", {})
    winner = bb.get("winner", {})
    loser = bb.get("loser", {})
    if winner:
        w_net = winner["net"]
        w_icon = PLUS if w_net > 0 else MINUS
        fields.append({
            "name": "\U0001f4aa Biggest Winner:",
            "value": f"{winner['team']}\n{w_icon}{abs(w_net):g} ({winner['record']})",
            "inline": True,
        })
    if loser:
        l_net = loser["net"]
        l_icon = PLUS if l_net > 0 else MINUS
        fields.append({
            "name": "\U0001f440 Biggest Loser:",
            "value": f"{loser['team']}\n{l_icon}{abs(l_net):g} ({loser['record']})",
            "inline": True,
        })
    if winner or loser:
        fields.append(SPACER)

    return fields


def _all_play_fields(stats: dict) -> list[dict]:
    weekly_ap = stats.get("weekly_all_play", {})
    luck = stats.get("luckiest_unluckiest", {})

    if not weekly_ap:
        return []

    teams = list(weekly_ap.items())
    best_name, best_rec = teams[0]
    worst_name, worst_rec = teams[-1]
    num_opponents = max(1, len(weekly_ap) - 1)

    def _normalize(rec):
        w = round(rec['wins'] / num_opponents)
        l = round(rec['losses'] / num_opponents)
        t = round(rec['ties'] / num_opponents)
        net = w - l + t * 0.5
        icon = PLUS if net >= 0 else MINUS
        return f"{icon}{abs(net):g} ({w}-{l}-{t})"

    fields = []
    fields.append({
        "name": "\U0001f525 Best vs. The Field:",
        "value": f"{best_name}\n{_normalize(best_rec)}",
        "inline": True,
    })
    fields.append({
        "name": "\U0001f4a9 Worst vs. The Field:",
        "value": f"{worst_name}\n{_normalize(worst_rec)}",
        "inline": True,
    })

    if luck.get("luckiest") or luck.get("unluckiest"):
        fields.append(SPACER)

    if luck.get("luckiest"):
        name, data = luck["luckiest"]
        fields.append({
            "name": "\U0001f340 Luckiest:",
            "value": f"{name}\nActual: {data['actual_record']} ({data['actual_pct']:.3f})\nShould Be: {data['all_play_record']} ({data['all_play_pct']:.3f})\nDiff: +{data['games_back']} games",
            "inline": True,
        })
    if luck.get("unluckiest"):
        name, data = luck["unluckiest"]
        fields.append({
            "name": "\U0001f622 Unluckiest:",
            "value": f"{name}\nActual: {data['actual_record']} ({data['actual_pct']:.3f})\nShould Be: {data['all_play_record']} ({data['all_play_pct']:.3f})\nDiff: {data['games_back']} games",
            "inline": True,
        })

    return fields


def _transactions_fields(stats: dict) -> list[dict]:
    txns = stats.get("most_transactions", [])
    if not txns:
        return []

    lines = [f"{t['count']} \u2013 {t['team']}" for t in txns[:5]]
    return [{
        "name": "\U0001f501 Most Transactions This Week:",
        "value": "\n".join(lines),
        "inline": False,
    }]


def _player_tag(player: dict) -> str:
    """Format a player as 'Name (POS-MLB) 🌱🔻'."""
    parts = []
    if player.get("position"):
        parts.append(player["position"])
    if player.get("mlb_team"):
        parts.append(player["mlb_team"])
    tag = "-".join(parts)
    suffix = f" ({tag})" if tag else ""
    badges = ""
    if player.get("rookie"):
        badges += " \U0001f331"  # 🌱
    if player.get("minors_eligible"):
        badges += " \U0001f53b"  # 🔻
    return f"{player['name']}{suffix}{badges}"


def format_transaction_embed(txn: dict) -> dict:
    """Format a single claim/drop transaction as a Discord embed.

    txn keys: tx_set_id, team_name, date, type, claim_type, added, dropped
    """
    claim_label = "Waiver" if txn.get("claim_type") == "WW" else "Free Agent"

    if txn["type"] == "claim_drop":
        footer_text = f"{claim_label} Claim"
    elif txn["type"] == "claim":
        footer_text = f"{claim_label} Add"
    else:
        footer_text = "Drop"

    lines = []
    if txn.get("added"):
        lines.append(f"{PLUS} {_player_tag(txn['added'])}")
    if txn.get("dropped"):
        lines.append(f"{MINUS} {_player_tag(txn['dropped'])}")

    footer_parts = []
    if txn.get("claim_type") == "WW":
        footer_parts.append(f"\u23f0 {footer_text}")
        if txn.get("waiver_priority"):
            footer_parts.append(f"#{txn['waiver_priority']} Priority")
    else:
        footer_parts.append(footer_text)
    footer_parts.append(txn.get("date", ""))

    embed = {
        "color": EMBED_COLOR,
        "author": {"name": txn["team_name"]},
        "description": "\n".join(lines),
        "footer": {"text": "  •  ".join(footer_parts)},
        "image": {"url": WHITESPACE_IMG},
    }
    headshot = (txn.get("added") or txn.get("dropped") or {}).get("headshot", "")
    if headshot:
        embed["thumbnail"] = {"url": headshot}
    return embed


def format_trade_embed(trade: dict) -> dict:
    """Format a trade as a Discord embed.

    Shows what each team gets. Draft picks display as '2026 Round 10 Pick'.
    trade keys: tx_set_id, date, players (list with name, position, mlb_team, from_team, to_team)
    """
    from collections import defaultdict
    received = defaultdict(list)

    for p in trade["players"]:
        received[p["to_team"]].append(p)

    fields = []
    for team, players in received.items():
        # Sort: real players first, then draft picks by round number (lowest first)
        players.sort(key=lambda p: (
            p.get("is_draft_pick", False),
            int(p["name"].split("Round ")[1].split(" ")[0]) if p.get("is_draft_pick") else 0,
        ))
        lines = []
        for p in players:
            icon = "\U0001f3f7\ufe0f" if p.get("is_draft_pick") else TRADE
            lines.append(f"{icon} {_player_tag(p)}")
        fields.append({
            "name": f"{team} Gets:",
            "value": "\n".join(lines),
            "inline": True,
        })

    return {
        "color": EMBED_COLOR,
        "author": {"name": "Trade"},
        "fields": fields,
        "footer": {"text": trade.get("date", "")},
        "image": {"url": WHITESPACE_IMG},
    }


def _streaks_fields(stats: dict) -> list[dict]:
    streaks = stats.get("streaks", {})

    win_streaks = sorted(
        [(t, s) for t, s in streaks.items() if s["type"] == "W" and s["count"] > 1],
        key=lambda x: x[1]["count"], reverse=True,
    )
    loss_streaks = sorted(
        [(t, s) for t, s in streaks.items() if s["type"] == "L" and s["count"] > 1],
        key=lambda x: x[1]["count"], reverse=True,
    )

    fields = []
    if win_streaks:
        lines = [f"{s['count']} \u2013 {team}" for team, s in win_streaks[:3]]
        fields.append({
            "name": "\U0001f4c8 Win Streak:",
            "value": "\n".join(lines),
            "inline": True,
        })
    if loss_streaks:
        lines = [f"{s['count']} \u2013 {team}" for team, s in loss_streaks[:3]]
        fields.append({
            "name": "\U0001f4c9 Loss Streak:",
            "value": "\n".join(lines),
            "inline": True,
        })
    if fields:
        fields.append(SPACER)

    return fields
