"""2026 Draft Grades - grade each manager's draft based on ADP surplus.

Usage:
    python draft_grade.py --dry-run          # Compute grades, save to draft_grades.json
    python draft_grade.py --post grades.json # Post finalized embed to Discord
"""
import argparse
import json
import math
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

load_dotenv()

from clients.fantrax_client import FantraxClient

LEAGUE_2026 = os.environ.get("FANTRAX_LEAGUE_ID_2026", "s41y9u1cmlpnnwv5")
EMBED_COLOR = 0x0099FF

GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-"]


def fetch_all_rosters(client: FantraxClient) -> dict[str, dict]:
    """Fetch rosters for all teams. Returns {scorer_id: {adp, team_id, team_name}}."""
    data = client._call("getTeamRosterInfo")
    teams = {ft["id"]: ft["name"] for ft in data.get("fantasyTeams", [])}

    def _parse_roster(data: dict, team_id: str) -> list[dict]:
        team_name = teams.get(team_id, team_id)
        players = []
        for table in data.get("tables", []):
            header = table.get("header", {}).get("cells", [])
            col_names = [c.get("shortName", c.get("name", "?")) for c in header]
            adp_idx = next((i for i, n in enumerate(col_names) if n == "ADP"), None)

            for row in table.get("rows", []):
                scorer = row.get("scorer", {})
                if not scorer.get("name"):
                    continue
                cells = row.get("cells", [])
                adp = ""
                if adp_idx is not None and adp_idx < len(cells):
                    adp = cells[adp_idx].get("content", "")
                players.append({
                    "scorer_id": scorer.get("scorerId", ""),
                    "player_name": scorer.get("name", "Unknown"),
                    "team_id": team_id,
                    "team_name": team_name,
                    "adp": adp,
                })
        return players

    first_team_id = data.get("displayedSelections", {}).get("displayedFantasyTeamId", "")
    all_players = _parse_roster(data, first_team_id)
    remaining = [tid for tid in teams if tid != first_team_id]

    def _fetch_team(tid):
        c = FantraxClient(client.league_id)
        d = c._call("getTeamRosterInfo", teamId=tid)
        return _parse_roster(d, tid)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for players in pool.map(_fetch_team, remaining):
            all_players.extend(players)

    return {p["scorer_id"]: p for p in all_players if p["scorer_id"]}


def fetch_projected_scores(client: FantraxClient) -> dict[str, str]:
    """Fetch projected scores. Returns {scorer_id: score}."""
    data = client._call(
        "getPlayerStats",
        statusOrTeamFilter="ALL_TAKEN",
        maxResultsPerPage="500",
    )
    header = data.get("tableHeader", {}).get("cells", [])
    col_keys = [c.get("key", "") for c in header]
    score_idx = next((i for i, k in enumerate(col_keys) if k == "score"), None)

    scores = {}
    for row in data.get("statsTable", []):
        scorer = row.get("scorer", {})
        sid = scorer.get("scorerId", "")
        if not sid or score_idx is None:
            continue
        cells = row.get("cells", [])
        if score_idx < len(cells):
            scores[sid] = cells[score_idx].get("content", "")
    return scores


def _build_expected_scores(draft_picks: list[dict], projected_scores: dict[str, str]) -> dict[int, float]:
    """Build a baseline of expected projected score at each draft slot.

    Uses a rolling average (window of 12 picks = one round) smoothed across
    all draft positions to establish what score you "should" get at each pick.
    """
    # Collect (overall_pick, score) for non-keeper picks only
    # Keepers are slotted into late rounds despite being elite players,
    # which would skew the expected score curve
    pick_scores = []
    for p in draft_picks:
        if p.get("keeper"):
            continue
        try:
            score = float(projected_scores.get(p["scorer_id"], ""))
        except (ValueError, TypeError):
            continue
        if score > 0:
            pick_scores.append((p["overall_pick"], score))

    pick_scores.sort()
    if not pick_scores:
        return {}

    # Rolling average with window = num_teams (one full round)
    num_teams = len({p["team_id"] for p in draft_picks}) or 12
    window = num_teams
    expected = {}
    for i, (pick_num, _) in enumerate(pick_scores):
        start = max(0, i - window // 2)
        end = min(len(pick_scores), i + window // 2 + 1)
        avg = sum(s for _, s in pick_scores[start:end]) / (end - start)
        expected[pick_num] = avg

    return expected


def compute_grades(draft_picks: list[dict], roster_adp: dict[str, dict],
                   projected_scores: dict[str, str]) -> list[dict]:
    """Compute draft grades for each team.

    Grades based on projected score surplus: how much better/worse each pick's
    projected score is vs the expected score at that draft slot.
    Weighted by 1/sqrt(round) so early-round value matters more.

    Returns list of team grade dicts sorted best to worst.
    """
    # Extract keeper ADPs for adjusted expected score curve
    keeper_adps = []
    for pick in draft_picks:
        if not pick.get("keeper"):
            continue
        roster = roster_adp.get(pick["scorer_id"], {})
        try:
            adp = float(roster.get("adp", ""))
            keeper_adps.append(adp)
        except (ValueError, TypeError):
            pass
    sorted_keeper_adps = sorted(keeper_adps)

    # Single curve for both grades and steal/reach (steal/reach uses effective_pick to look up)
    expected_scores = _build_expected_scores(draft_picks, projected_scores)

    def _effective_pick(overall: int) -> int:
        count = 0
        for adp in sorted_keeper_adps:
            if adp <= overall:
                count += 1
            else:
                break
        return overall + count

    # Group picks by team
    team_picks: dict[str, list[dict]] = {}
    for pick in draft_picks:
        tid = pick["team_id"]
        if tid not in team_picks:
            team_picks[tid] = []

        sid = pick["scorer_id"]
        overall = pick["overall_pick"]

        # Parse projected score
        try:
            proj_score = float(projected_scores.get(sid, ""))
        except (ValueError, TypeError):
            proj_score = None

        # Expected score at this draft slot (unadjusted, for grades)
        expected = expected_scores.get(overall)

        # Score surplus: positive = got a better player than expected
        if proj_score is not None and proj_score > 0 and expected is not None:
            surplus = proj_score - expected
        else:
            surplus = None

        # Round weight
        weight = 1 / math.sqrt(pick["round"])

        # ADP for display and steal/reach calculation
        roster = roster_adp.get(sid, {})
        try:
            adp = float(roster.get("adp", ""))
        except (ValueError, TypeError):
            adp = None

        # ADP surplus for steal/reach labels
        # Compares effective draft position (adjusted for keepers) vs ADP
        # Positive = steal (got them later than ADP), negative = reach
        eff = _effective_pick(overall)
        if adp is not None and not pick.get("keeper"):
            adp_surplus = eff - adp
        else:
            adp_surplus = None

        team_picks[tid].append({
            "player_name": pick["player_name"],
            "position": pick["position"],
            "round": pick["round"],
            "pick": pick["pick"],
            "overall_pick": overall,
            "adp": adp,
            "projected_score": proj_score,
            "expected_score": round(expected, 1) if expected is not None else None,
            "score_surplus": round(surplus, 1) if surplus is not None else None,
            "weighted_surplus": round(surplus * weight, 2) if surplus is not None else None,
            "adp_surplus": round(adp_surplus, 1) if adp_surplus is not None else None,
            "keeper": pick.get("keeper", False),
        })

    # Compute team scores
    team_grades = []
    for tid, picks in team_picks.items():
        weighted_total = sum(
            p["weighted_surplus"] for p in picks if p["weighted_surplus"] is not None
        )
        # Exclude keepers; limit to first 10 picks per team for steal/reach
        first_10 = [p for p in picks if p["adp_surplus"] is not None and not p["keeper"] and p["round"] <= 10]

        biggest_steal = max(first_10, key=lambda p: p["adp_surplus"]) if first_10 else None
        biggest_reach = min(first_10, key=lambda p: p["adp_surplus"]) if first_10 else None

        team_name = picks[0]["player_name"]  # fallback
        for p in draft_picks:
            if p["team_id"] == tid:
                team_name = p["team_name"]
                break

        def _pick_summary(p):
            if p is None:
                return None
            return {
                "player_name": p["player_name"],
                "round": p["round"],
                "overall_pick": p["overall_pick"],
                "adp": p["adp"],
                "adp_surplus": p["adp_surplus"],
            }

        team_grades.append({
            "team_id": tid,
            "team_name": team_name,
            "score": round(weighted_total, 2),
            "picks": sorted(picks, key=lambda p: p["round"]),
            "biggest_steal": _pick_summary(biggest_steal),
            "biggest_reach": _pick_summary(biggest_reach),
            "comment": _generate_comment(picks, team_name),
        })

    # Sort best to worst, assign grades
    team_grades.sort(key=lambda t: t["score"], reverse=True)
    for i, team in enumerate(team_grades):
        if i < len(GRADE_ORDER):
            team["grade"] = GRADE_ORDER[i]
        else:
            team["grade"] = "D-"

    return team_grades


def _generate_comment(picks: list[dict], team_name: str) -> str:
    """Generate a roast/comment analyzing the team's draft tendencies."""
    non_keeper = [p for p in picks if not p.get("keeper")]
    early = [p for p in non_keeper if p["round"] <= 5]
    # Use team name hash for deterministic but varied phrase selection
    _h = hash(team_name)

    def _pick(options: list[str]) -> str:
        return options[_h % len(options)]

    observations = []

    # Position doubles in early rounds (first 5)
    early_positions = Counter()
    for p in early:
        for pos in p["position"].split(","):
            early_positions[pos] += 1
    dupes = {pos: n for pos, n in early_positions.items()
             if pos not in ("SP", "RP", "OF", "UT") and n >= 2}
    if dupes:
        parts = " and ".join(f"{n} {pos}s" for pos, n in sorted(dupes.items(), key=lambda x: -x[1]))
        observations.append(_pick([
            f"{parts} in the first 5 rounds is wild",
            f"Doubled up on {parts} early like roster spots don't matter",
            f"{parts} before round 6? Read the room",
            f"Nobody needed {parts} that early but here we are",
            f"Burning early capital on {parts} is a fireable offense",
        ]))

    # Count keeper positions for context
    keepers = [p for p in picks if p.get("keeper")]
    keeper_sp = len([p for p in keepers if "SP" in p["position"]])
    keeper_rp = len([p for p in keepers if "RP" in p["position"] and "SP" not in p["position"]])
    keeper_pitchers = keeper_sp + keeper_rp

    # Pitcher avoidance - only flag if they don't have keeper pitchers to justify it
    first_pitcher = next((p for p in non_keeper if "SP" in p["position"] or "RP" in p["position"]), None)
    if first_pitcher and keeper_pitchers == 0:
        if first_pitcher["round"] >= 10:
            observations.append(_pick([
                f"Didn't draft a single pitcher until round {first_pitcher['round']}",
                f"First arm off the board in round {first_pitcher['round']}. Prayers up for that rotation",
                f"Round {first_pitcher['round']} for the first pitcher is negligence",
            ]))
        elif first_pitcher["round"] >= 7:
            observations.append(_pick([
                f"Waited until round {first_pitcher['round']} for a pitcher like ERA doesn't exist",
                f"First pitcher in round {first_pitcher['round']}. Bold strategy, let's see how it plays out",
                f"Punted pitching until round {first_pitcher['round']}",
            ]))

    # Pitcher heavy early - only flag at 4+ (3 is fine with 5 pitcher slots)
    early_pitchers = [p for p in early if "SP" in p["position"] or "RP" in p["position"]]
    if len(early_pitchers) >= 4:
        observations.append(_pick([
            f"Blew {len(early_pitchers)} of the first 5 picks on arms",
            f"{len(early_pitchers)} pitchers in the first 5 rounds, who's hitting?",
        ]))

    # SP/RP counts - account for keepers
    total_sp = len([p for p in non_keeper if "SP" in p["position"]]) + keeper_sp
    total_rp = len([p for p in non_keeper if "RP" in p["position"] and "SP" not in p["position"]]) + keeper_rp
    if total_rp == 0:
        observations.append(_pick([
            "Zero relievers, good luck in ratios",
            "No bullpen at all. Just vibes and prayer",
            "Didn't roster a single reliever. Bold and stupid",
        ]))
    elif total_rp >= 5:
        observations.append(_pick([
            f"{total_rp} relievers is unserious",
            f"Hoarding {total_rp} relievers like they're going extinct",
            f"{total_rp} damn relievers on the roster",
        ]))
    if total_sp >= 9:
        observations.append(_pick([
            f"{total_sp} starting pitchers is hoarding",
            f"{total_sp} SPs rostered. That's not a team, that's a pitching staff",
            f"Stockpiled {total_sp} starters like it's the apocalypse",
        ]))

    # ADP tendencies
    adp_picks = [p for p in non_keeper if p.get("adp_surplus") is not None]
    if adp_picks:
        avg_surplus = sum(p["adp_surplus"] for p in adp_picks) / len(adp_picks)
        big_reaches = [p for p in adp_picks if p["adp_surplus"] < -100]
        if len(big_reaches) >= 3:
            observations.append(_pick([
                f"{len(big_reaches)} picks went 100+ spots ahead of ADP. Unhinged",
                f"Reached 100+ spots on {len(big_reaches)} guys. Drafting off vibes not data",
                f"{len(big_reaches)} picks over 100 spots early. Somebody take the board away",
            ]))
        elif avg_surplus < -50:
            observations.append(_pick([
                "Reached on damn near every pick",
                "Consistently overdrafted the whole way through",
                "ADP was more of a suggestion apparently",
            ]))
        elif avg_surplus > 10:
            observations.append(_pick([
                "Played it safe all day, no conviction",
                "Value picks only, zero swing. Boring as hell",
            ]))

    # Single egregious reach in first 10 rounds
    early_adp = [p for p in adp_picks if p["round"] <= 10 and p.get("adp") is not None]
    if early_adp:
        worst_reach = min(early_adp, key=lambda p: p["adp_surplus"])
        if worst_reach["adp_surplus"] < -150:
            name = worst_reach["player_name"]
            rd = worst_reach["round"]
            adp = worst_reach["adp"]
            observations.append(_pick([
                f"{name} in round {rd} with an ADP of {adp:.0f} is criminal",
                f"Took {name} in round {rd}. ADP is {adp:.0f}. What are we doing",
                f"{name} at round {rd} (ADP {adp:.0f}) might be the worst pick in the draft",
                f"Round {rd} on {name}? His ADP is {adp:.0f}. Insane behavior",
            ]))

    # OF heavy - account for keeper OFs, flag at 7+ total
    keeper_of = len([p for p in keepers if "OF" in p["position"]])
    total_of = len([p for p in non_keeper if "OF" in p["position"]]) + keeper_of
    if total_of >= 7:
        observations.append(_pick([
            f"{total_of} outfielders is embarrassing",
            f"Rostered {total_of} outfielders like there's 5 OF slots",
            f"{total_of} outfielders. Bro thinks he's managing a rec league softball team",
        ]))

    # Team name roasts
    if "claude" in team_name.lower():
        observations.append("Named after me and still drafted like this? Embarrassing for both of us")

    # Fallback: identify weakest position group
    if not observations:
        groups: dict[str, list[float]] = {"C": [], "IF": [], "OF": [], "SP": [], "RP": []}
        all_picks = [p for p in picks if p.get("projected_score") and p["projected_score"] > 0]
        for p in all_picks:
            pos = p["position"]
            score = p["projected_score"]
            if "SP" in pos:
                groups["SP"].append(score)
            elif "RP" in pos:
                groups["RP"].append(score)
            elif "C" in pos.split(","):
                groups["C"].append(score)
            elif any(x in pos for x in ("1B", "2B", "3B", "SS")):
                groups["IF"].append(score)
            elif "OF" in pos:
                groups["OF"].append(score)

        group_avgs = {g: sum(s) / len(s) for g, s in groups.items() if s}
        if group_avgs:
            worst_group = min(group_avgs, key=lambda g: group_avgs[g])
            labels = {"C": "catcher", "IF": "infield", "OF": "outfield", "SP": "rotation", "RP": "bullpen"}
            group_label = labels.get(worst_group, worst_group)
            observations.append(_pick([
                f"Weak ass {group_label}",
                f"That {group_label} is embarrassing",
                f"Good luck with that {group_label}",
                f"The {group_label} is not it",
                f"That {group_label} is cooked",
            ]))

    return ". ".join(observations[:3]) + "." if observations else ""


def format_draft_grade_message(grades: list[dict]) -> list[str]:
    """Format draft grades as plain Discord messages. Splits at 2000 char limit."""
    lines = ["# 2026 Draft Grades\n"]
    for team in grades:
        steal = team.get("biggest_steal")
        reach = team.get("biggest_reach")
        comment = team.get("comment") or ""

        lines.append(f"### {team['grade']}  {team['team_name']}")
        if steal:
            lines.append(f"Best pick: **{steal['player_name']}** (Rd {steal['round']}, ADP {steal['adp']:.0f})")
        if reach:
            lines.append(f"Biggest reach: **{reach['player_name']}** (Rd {reach['round']}, ADP {reach['adp']:.0f})")
        if comment:
            lines.append(f"*{comment}*")
        lines.append("")

    # Split into messages under 2000 chars
    messages = []
    current = ""
    for line in lines:
        candidate = current + line + "\n"
        if len(candidate) > 1900 and current:
            messages.append(current.rstrip())
            current = line + "\n"
        else:
            current = candidate
    if current.strip():
        messages.append(current.rstrip())

    return messages


def main():
    parser = argparse.ArgumentParser(description="2026 Draft Grades")
    parser.add_argument("--dry-run", action="store_true", help="Compute grades and save to JSON")
    parser.add_argument("--post", metavar="FILE", help="Post grades from JSON file to Discord")
    parser.add_argument("--webhook-url", default=os.environ.get("DISCORD_WEBHOOK_URL"),
                        help="Discord webhook URL (default: DISCORD_WEBHOOK_URL env var)")
    args = parser.parse_args()

    if not args.dry_run and not args.post:
        parser.error("Must specify --dry-run or --post <file>")

    if args.post:
        # Post mode: read JSON and send to Discord
        if not args.webhook_url:
            print("Error: --webhook-url or DISCORD_WEBHOOK_URL required", file=sys.stderr)
            sys.exit(1)

        with open(args.post) as f:
            grades = json.load(f)

        messages = format_draft_grade_message(grades)
        for i, msg in enumerate(messages):
            resp = requests.post(args.webhook_url, json={"content": msg})
            if resp.status_code == 204:
                print(f"Posted message {i + 1}/{len(messages)} to Discord")
            else:
                print(f"Discord error {resp.status_code}: {resp.text}", file=sys.stderr)
                resp.raise_for_status()
        return

    # Dry-run mode: compute grades
    client = FantraxClient(LEAGUE_2026)

    print("Fetching draft results, rosters, and projected scores...", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_draft = pool.submit(client.draft_results)
        fut_rosters = pool.submit(fetch_all_rosters, client)
        fut_scores = pool.submit(fetch_projected_scores, client)

        draft_picks = fut_draft.result()
        roster_adp = fut_rosters.result()
        projected_scores = fut_scores.result()

    print(f"Got {len(draft_picks)} picks, {len(roster_adp)} roster entries, "
          f"{len(projected_scores)} projected scores", file=sys.stderr)

    grades = compute_grades(draft_picks, roster_adp, projected_scores)

    # Print summary
    print("\n2026 Draft Grades\n" + "=" * 40)
    for team in grades:
        steal = team.get("biggest_steal")
        reach = team.get("biggest_reach")
        print(f"\n{team['grade']:>3}  {team['team_name']} (score: {team['score']:+.1f})")
        if steal:
            print(f"     Best pick: {steal['player_name']} (Rd {steal['round']}, ADP {steal['adp']:.0f})")
        if reach:
            print(f"     Biggest reach: {reach['player_name']} (Rd {reach['round']}, ADP {reach['adp']:.0f})")

    # Save to JSON
    out_file = "draft_grades.json"
    with open(out_file, "w") as f:
        json.dump(grades, f, indent=2)
    print(f"\nSaved to {out_file}", file=sys.stderr)

    # Preview message
    messages = format_draft_grade_message(grades)
    print(f"\nMessage preview ({len(messages)} message(s)):", file=sys.stderr)
    for i, msg in enumerate(messages):
        print(f"\n--- Message {i + 1} ({len(msg)} chars) ---")
        print(msg)


if __name__ == "__main__":
    main()
