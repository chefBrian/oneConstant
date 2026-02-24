"""Stat calculations for H2H Categories weekly recaps."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from fantrax_client import FantraxClient

# Categories where lower is better
LOWER_IS_BETTER = {"Earned Run Average", "WHIP Ratio", "Walks Allowed Per Nine Innings",
                   "Losses", "Home Runs Allowed"}


def compute_weekly_stats(client: FantraxClient, period_num: int | None = None) -> dict:
    """Compute all weekly recap stats for a given period.

    If period_num is None, uses the latest completed period.
    """
    schedule = client.schedule()

    if period_num is None:
        period = _latest_completed(schedule)
    else:
        period = next((p for p in schedule if p["period_num"] == period_num), None)

    if not period or not period["matchups"]:
        return {"error": "No completed period found"}

    # Batch fetch standings + prev standings + transactions in one API call
    batch = client.fetch_period_data(period["period_num"])
    standings = batch["standings"]
    prev_standings = batch["prev_standings"]
    txns = batch["transactions"]

    return {
        "period": period,
        "total_periods": len(schedule),
        "standings": standings,
        "standings_movement": _standings_movement(standings, prev_standings),
        "biggest_blowout": _biggest_blowout(period),
        "dominant_performance": _dominant_performance(period),
        "category_kings": _category_kings(period),
        "all_play_record": _all_play_record(schedule, period["period_num"]),
        "weekly_all_play": _weekly_all_play(period),
        "streaks": _streaks(schedule, period["period_num"]),
        "luckiest_unluckiest": _luck_rating(standings, schedule, period["period_num"]),
        "category_sweeps": _category_sweeps(period),
        "most_transactions": _most_transactions_from_data(txns, period),
    }


def _latest_completed(schedule: list[dict]) -> dict | None:
    latest = None
    for p in schedule:
        if not p["matchups"]:
            continue
        m = p["matchups"][0]
        if m["away_wins"] + m["away_losses"] + m["away_ties"] > 0:
            latest = p
    return latest


def _biggest_blowout(period: dict) -> dict:
    """Find the matchup with the largest category win differential."""
    best_winner = None
    best_win_net = -999
    best_loser = None
    best_lose_net = 999
    for m in period["matchups"]:
        for side, opp in [("away", "home"), ("home", "away")]:
            w, l, t = m[f"{side}_wins"], m[f"{side}_losses"], m[f"{side}_ties"]
            net = w - l + t * 0.5
            record = f"{w}-{l}-{t}"
            team = m[f"{side}_team_name"]
            if net > best_win_net:
                best_win_net = net
                best_winner = {"team": team, "record": record, "net": net}
            if net < best_lose_net:
                best_lose_net = net
                best_loser = {"team": team, "record": record, "net": net}
    return {
        "winner": best_winner or {},
        "loser": best_loser or {},
    }


def _dominant_performance(period: dict) -> dict:
    """Find the team that won the most categories in a single matchup."""
    best = None
    best_wins = 0
    for m in period["matchups"]:
        for side in ["away", "home"]:
            wins = m[f"{side}_wins"]
            if wins > best_wins:
                best_wins = wins
                opp = "home" if side == "away" else "away"
                best = {
                    "team": m[f"{side}_team_name"],
                    "opponent": m[f"{opp}_team_name"],
                    "wins": wins,
                    "losses": m[f"{side}_losses"],
                    "ties": m[f"{side}_ties"],
                }
    return best or {}


def _category_kings(period: dict) -> dict:
    """For each stat category, find who had the best value across all teams."""
    if not period["matchups"]:
        return {}

    categories = period["matchups"][0]["categories"]
    # Collect all team values per category
    team_values = defaultdict(list)

    lower_is_better = LOWER_IS_BETTER

    for m in period["matchups"]:
        for side in ["away", "home"]:
            team_name = m[f"{side}_team_name"]
            cats = m[f"{side}_cats"]
            for cat_name in categories:
                val_str = cats[cat_name]["value"]
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                team_values[cat_name].append((team_name, val))

    kings = {}
    for cat_name, values in team_values.items():
        if not values:
            continue
        reverse = cat_name not in lower_is_better
        sorted_vals = sorted(values, key=lambda x: x[1], reverse=reverse)
        kings[cat_name] = {"team": sorted_vals[0][0], "value": sorted_vals[0][1]}

    return kings


def _collect_team_cats(period: dict) -> dict[str, dict[str, float]]:
    """Collect each team's category values for a period.

    Returns {team_name: {cat_name: float_value, ...}, ...}
    """
    team_cats = {}
    for m in period["matchups"]:
        for side in ("away", "home"):
            name = m[f"{side}_team_name"]
            cats = m[f"{side}_cats"]
            team_cats[name] = {}
            for cat, info in cats.items():
                try:
                    team_cats[name][cat] = float(info["value"])
                except (ValueError, TypeError):
                    team_cats[name][cat] = 0.0
            # If team has 0 IP, penalize lower-is-better pitching cats
            if team_cats[name].get("Innings Pitched", 0) == 0:
                for cat in LOWER_IS_BETTER:
                    if cat in team_cats[name]:
                        team_cats[name][cat] = float("inf")
    return team_cats


def _simulate_h2h(team1_cats: dict, team2_cats: dict) -> tuple[int, int, int]:
    """Simulate a H2H category matchup between two teams.

    Returns (team1_wins, team1_losses, team1_ties).
    """
    wins, losses, ties = 0, 0, 0
    for cat in team1_cats:
        v1 = team1_cats[cat]
        v2 = team2_cats.get(cat, 0.0)
        if cat in LOWER_IS_BETTER:
            v1, v2 = -v1, -v2  # flip so higher is always better
        if v1 > v2:
            wins += 1
        elif v1 < v2:
            losses += 1
        else:
            ties += 1
    return wins, losses, ties


def _all_play_record(schedule: list[dict], through_period: int) -> dict[str, dict]:
    """Calculate each team's all-play record through a given period.

    For each week, simulate H2H category matchups against every other team.
    """
    records = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})

    for period in schedule:
        if period["period_num"] > through_period:
            break
        if not period["matchups"]:
            continue

        team_cats = _collect_team_cats(period)
        teams = list(team_cats.keys())

        for i, t1 in enumerate(teams):
            for t2 in teams[i + 1:]:
                w, l, t = _simulate_h2h(team_cats[t1], team_cats[t2])
                records[t1]["wins"] += w
                records[t1]["losses"] += l
                records[t1]["ties"] += t
                records[t2]["wins"] += l
                records[t2]["losses"] += w
                records[t2]["ties"] += t

    return dict(sorted(records.items(),
                        key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"] + x[1]["ties"]),
                        reverse=True))


def _weekly_all_play(period: dict) -> dict[str, dict]:
    """All-play record for just this week using category-by-category H2H."""
    team_cats = _collect_team_cats(period)
    teams = list(team_cats.keys())

    records = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
    for i, t1 in enumerate(teams):
        for t2 in teams[i + 1:]:
            w, l, t = _simulate_h2h(team_cats[t1], team_cats[t2])
            records[t1]["wins"] += w
            records[t1]["losses"] += l
            records[t1]["ties"] += t
            records[t2]["wins"] += l
            records[t2]["losses"] += w
            records[t2]["ties"] += t

    return dict(sorted(records.items(),
                        key=lambda x: x[1]["wins"], reverse=True))


def _streaks(schedule: list[dict], through_period: int) -> dict[str, dict]:
    """Calculate current winning/losing streaks."""
    # Build per-team win/loss history
    history = defaultdict(list)  # team_name -> list of "W"/"L"/"T" per period

    for period in schedule:
        if period["period_num"] > through_period:
            break
        if not period["matchups"]:
            continue
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

    streaks = {}
    for team, results in history.items():
        if not results:
            continue
        current = results[-1]
        count = 0
        for r in reversed(results):
            if r == current:
                count += 1
            else:
                break
        streaks[team] = {"type": current, "count": count}

    return streaks


def _standings_movement(current: list[dict], previous: list[dict] | None) -> dict[str, int]:
    """Calculate how many spots each team moved in standings."""
    if not previous:
        return {}

    prev_ranks = {s["team_name"]: s["rank"] for s in previous}
    movement = {}
    for s in current:
        prev = prev_ranks.get(s["team_name"], s["rank"])
        movement[s["team_name"]] = prev - s["rank"]  # positive = moved up
    return movement


def _luck_rating(standings: list[dict], schedule: list[dict], through_period: int) -> dict:
    """Find luckiest and unluckiest teams based on actual record vs all-play record."""
    all_play = _all_play_record(schedule, through_period)
    num_opponents = max(1, len(standings) - 1)

    luck = {}
    for s in standings:
        name = s["team_name"]
        actual_total = s["wins"] + s["losses"] + s["ties"]
        actual_pct = s["wins"] / max(1, actual_total)
        if name in all_play:
            ap = all_play[name]
            ap_w = ap["wins"]
            ap_l = ap["losses"]
            ap_t = ap["ties"]
            ap_pct = ap_w / max(1, ap_w + ap_l + ap_t)
            # Normalize all-play categories to per-opponent matchup-equivalent record
            disp_ap_w = round(ap_w / num_opponents)
            disp_ap_l = round(ap_l / num_opponents)
            disp_ap_t = round(ap_t / num_opponents)
            games_back = s["wins"] - disp_ap_w
            luck[name] = {
                "actual_pct": actual_pct,
                "all_play_pct": ap_pct,
                "diff": actual_pct - ap_pct,
                "games_back": games_back,
                "actual_record": f"{s['wins']}-{s['losses']}-{s['ties']}",
                "all_play_record": f"{disp_ap_w}-{disp_ap_l}-{disp_ap_t}",
            }

    sorted_luck = sorted(luck.items(), key=lambda x: x[1]["games_back"], reverse=True)
    return {
        "luckiest": (sorted_luck[0][0], sorted_luck[0][1]) if sorted_luck else None,
        "unluckiest": (sorted_luck[-1][0], sorted_luck[-1][1]) if sorted_luck else None,
    }


def _category_sweeps(period: dict) -> list[dict]:
    """Find any near-sweeps (winning 15+ of 18 categories)."""
    sweeps = []
    for m in period["matchups"]:
        total_cats = m["away_wins"] + m["away_losses"] + m["away_ties"]
        if total_cats == 0:
            continue
        for side in ["away", "home"]:
            wins = m[f"{side}_wins"]
            if wins >= total_cats * 0.8:  # Won 80%+ of categories
                opp = "home" if side == "away" else "away"
                sweeps.append({
                    "team": m[f"{side}_team_name"],
                    "opponent": m[f"{opp}_team_name"],
                    "wins": wins,
                    "total": total_cats,
                })
    return sweeps


def _most_transactions_from_data(txns: list[dict], period: dict) -> list[dict]:
    """Count transactions per team during the scoring period, return sorted."""
    date_range = period["date_range"]  # "(Mon Jun 16, 2025 - Sun Jun 22, 2025)"
    stripped = date_range.strip("()")
    start_str, end_str = stripped.split(" - ")
    start = datetime.strptime(start_str.strip(), "%a %b %d, %Y")
    end = datetime.strptime(end_str.strip(), "%a %b %d, %Y").replace(hour=23, minute=59, second=59)

    counts = Counter()
    for t in txns:
        if not t.get("date"):
            continue
        try:
            parts = t["date"].split(",")
            txn_date = datetime.strptime(parts[0].strip() + "," + parts[1].strip(), "%a %b %d, %Y")
        except (ValueError, IndexError):
            continue
        if start <= txn_date <= end:
            counts[t["team_name"]] += 1

    return [{"team": team, "count": count} for team, count in counts.most_common() if count > 0]


if __name__ == "__main__":
    client = FantraxClient("uo0es7lom23shg6b")
    stats = compute_weekly_stats(client, period_num=10)

    print(f"Period: {stats['period']['name']} {stats['period']['date_range']}")
    print()

    bb = stats["biggest_blowout"]
    w, l = bb.get("winner", {}), bb.get("loser", {})
    print(f"BIGGEST WINNER: {w.get('team')} +{w.get('net', 0):g} ({w.get('record')})")
    print(f"BIGGEST LOSER: {l.get('team')} {l.get('net', 0):g} ({l.get('record')})")

    dp = stats["dominant_performance"]
    print(f"DOMINANT: {dp['team']} won {dp['wins']} cats vs {dp['opponent']}")
    print()

    print("STANDINGS MOVEMENT:")
    for team, move in stats["standings_movement"].items():
        arrow = "^" * move if move > 0 else "v" * abs(move) if move < 0 else "-"
        print(f"  {team}: {arrow}")
    print()

    print("STREAKS:")
    for team, streak in sorted(stats["streaks"].items(), key=lambda x: x[1]["count"], reverse=True):
        print(f"  {team}: {streak['count']}{streak['type']}")
    print()

    print("WEEKLY ALL-PLAY:")
    for team, rec in stats["weekly_all_play"].items():
        print(f"  {team}: {rec['wins']}-{rec['losses']}-{rec['ties']}")
    print()

    luck = stats["luckiest_unluckiest"]
    if luck["luckiest"]:
        name, data = luck["luckiest"]
        print(f"LUCKIEST: {name} (actual {data['actual_record']}, all-play {data['all_play_record']}, diff +{data['diff']:.3f})")
    if luck["unluckiest"]:
        name, data = luck["unluckiest"]
        print(f"UNLUCKIEST: {name} (actual {data['actual_record']}, all-play {data['all_play_record']}, diff {data['diff']:.3f})")
    print()

    if stats["category_sweeps"]:
        print("CATEGORY SWEEPS:")
        for s in stats["category_sweeps"]:
            print(f"  {s['team']} dominated {s['opponent']}: {s['wins']}/{s['total']} cats")
