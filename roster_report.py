"""Roster report: all rostered players with ADP, 2025 score, and draft history.

Outputs a table with:
  Fantasy Team | Player | ADP | Proj Round | 2025 Score | 2025 Draft

Usage:
    python roster_report.py              # Pretty table
    python roster_report.py --sheets     # Write to Google Sheets
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()

from fantrax_client import FantraxClient

SPREADSHEET_ID = "1EQrueJkYowhMbgkDfHmy7KJd2CeIkLn9vRI7sIkQC9g"

# 2026 league has current rosters + ADP in pre-season projection view
LEAGUE_2026 = os.environ.get("FANTRAX_LEAGUE_ID_2026", "s41y9u1cmlpnnwv5")
# 2025 league has last year's draft results
LEAGUE_2025 = os.environ.get("FANTRAX_LEAGUE_ID", "uo0es7lom23shg6b")


def fetch_all_rosters(client: FantraxClient) -> tuple[list[dict], dict[str, str]]:
    """Fetch rosters for all teams. Returns (players, team_id_to_name)."""
    # First call to get team list and header structure
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
                    continue  # empty roster slot
                cells = row.get("cells", [])
                adp = ""
                if adp_idx is not None and adp_idx < len(cells):
                    adp = cells[adp_idx].get("content", "")

                players.append({
                    "team_name": team_name,
                    "player_name": scorer.get("name", "Unknown"),
                    "scorer_id": scorer.get("scorerId", ""),
                    "position": scorer.get("posShortNames", ""),
                    "mlb_team": scorer.get("teamShortName", ""),
                    "adp": adp,
                })
        return players

    # Parse the first team (already fetched)
    first_team_id = data.get("displayedSelections", {}).get("displayedFantasyTeamId", "")
    all_players = _parse_roster(data, first_team_id)
    remaining_team_ids = [tid for tid in teams if tid != first_team_id]

    # Fetch remaining teams in parallel
    def _fetch_team(tid):
        c = FantraxClient(client.league_id)
        d = c._call("getTeamRosterInfo", teamId=tid)
        return _parse_roster(d, tid)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(_fetch_team, remaining_team_ids)
        for players in results:
            all_players.extend(players)

    return all_players, teams


def fetch_player_scores(client: FantraxClient) -> dict[str, dict]:
    """Fetch 2025 player scores for all rostered players.

    Uses getPlayerStats with ALL_TAKEN filter. Returns {scorer_id: {score, rank}}.
    """
    data = client._call(
        "getPlayerStats",
        statusOrTeamFilter="ALL_TAKEN",
        maxResultsPerPage="500",
    )
    header = data.get("tableHeader", {}).get("cells", [])
    col_keys = [c.get("key", "") for c in header]
    score_idx = next((i for i, k in enumerate(col_keys) if k == "score"), None)
    rank_idx = next((i for i, k in enumerate(col_keys) if k == "rankOv"), None)

    score_map = {}
    for row in data.get("statsTable", []):
        scorer = row.get("scorer", {})
        sid = scorer.get("scorerId", "")
        if not sid:
            continue
        cells = row.get("cells", [])
        score = cells[score_idx].get("content", "") if score_idx is not None else ""
        rank = cells[rank_idx].get("content", "") if rank_idx is not None else ""
        score_map[sid] = {"score": score, "rank": rank}

    return score_map


def fetch_draft_results(client: FantraxClient) -> dict[str, dict]:
    """Fetch draft results. Returns {scorer_id: {round, pick}}."""
    data = client._call("getDraftResults")
    picks = data.get("draftPicksOrdered", [])

    draft_map = {}
    for pick in picks:
        sid = pick.get("scorerId")
        if sid:
            draft_map[sid] = {
                "round": pick["round"],
                "pick": pick["pickNumber"],
            }
    return draft_map


def fetch_keeper_history(spreadsheet) -> dict[str, dict]:
    """Read 2025 keeper list sheet. Returns {player_name: {kept, keeper_cost}}.

    Any player present in the sheet was kept in 2025.
    Keeper cost is -2 per year kept. 2024 Bump tells us prior years:
      bump 0 = first time kept (in 2025), so 2026 cost = -4 (2nd year)
      bump 2 = kept once before 2025, so 2026 cost = -4 (2nd year)
      bump 4 = kept twice before, so 2026 cost = -6 (3rd year)
      bump 6 = kept three times before, so 2026 cost = -8 (4th year)
    2026 Keeper Round = 2025 Draft Round - keeper_cost
    """
    ws = spreadsheet.worksheet("2025 keeper list")
    all_vals = ws.get_all_values()
    header = all_vals[0]

    player_col = header.index("Player")
    bump_col = header.index("2024 Bump")

    keeper_map = {}
    for row in all_vals[1:]:
        name = row[player_col].strip()
        if not name:
            continue

        try:
            prev_bump = int(row[bump_col]) if row[bump_col] else 0
        except ValueError:
            prev_bump = 0

        # 2026 keeper cost = prior bump + 2 (kept in 2025) + 2 (kept in 2026)
        keeper_cost = prev_bump + 2 + 2

        keeper_map[name] = {
            "kept": True,
            "keeper_cost": keeper_cost,
        }

    return keeper_map


def main():
    parser = argparse.ArgumentParser(description="Roster report with ADP and draft history")
    parser.add_argument("--sheets", action="store_true", help="Write to Google Sheets")
    args = parser.parse_args()

    import google.auth
    import gspread

    client_2026 = FantraxClient(LEAGUE_2026)
    client_2025 = FantraxClient(LEAGUE_2025)

    # Auth for Google Sheets (needed for reading keeper history + optional writing)
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path:
        gc = gspread.service_account(filename=creds_path)
    else:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds, _ = google.auth.default(scopes=scopes)
        gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    print("Fetching rosters, scores, draft, and keeper data...", file=sys.stderr)

    # Fetch in parallel
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_rosters = pool.submit(fetch_all_rosters, client_2026)
        fut_scores = pool.submit(fetch_player_scores, client_2025)
        fut_draft = pool.submit(fetch_draft_results, client_2025)
        fut_keepers = pool.submit(fetch_keeper_history, spreadsheet)

        players, teams = fut_rosters.result()
        scores_2025 = fut_scores.result()
        draft_2025 = fut_draft.result()
        keeper_history = fut_keepers.result()

    print(f"Found {len(players)} rostered players across {len(teams)} teams", file=sys.stderr)

    # Merge draft and score data onto players by scorer_id
    rows = []
    for p in players:
        draft = draft_2025.get(p["scorer_id"])
        score_info = scores_2025.get(p["scorer_id"], {})
        # Condense player name with position and MLB team
        parts = [p["player_name"]]
        detail = " - ".join(filter(None, [p["position"], p["mlb_team"]]))
        if detail:
            parts.append(f"({detail})")
        player_str = " ".join(parts)

        # Projected draft round from ADP (12-team league)
        try:
            proj_round = int(float(p["adp"]) // 12) + 1 if p["adp"] else ""
        except ValueError:
            proj_round = ""

        # Keeper history from 2025 keeper list sheet
        keeper = keeper_history.get(p["player_name"], {})
        keeper_cost_val = keeper.get("keeper_cost", 0)
        if keeper_cost_val:
            times_kept = keeper_cost_val // 2 - 1
            keeper_cost = f"-{keeper_cost_val} ({times_kept}x kept)"
        else:
            keeper_cost = ""

        # 2026 Keeper Round = 2025 Draft Round - keeper cost
        # Undrafted players are assigned round 21 (no subtraction)
        if draft:
            draft_round = draft["round"]
            if keeper_cost_val:
                keeper_rnd_2026 = draft_round - keeper_cost_val
            else:
                keeper_rnd_2026 = draft_round - 2
        else:
            keeper_rnd_2026 = 21
        keeper_rnd_2026 = str(keeper_rnd_2026) if keeper_rnd_2026 >= 1 else "N/A"

        # Keeper value: weighted surplus (round savings * talent weight)
        # Higher = more valuable keeper. N/A keepers get empty.
        try:
            kr = int(keeper_rnd_2026)
            pr = int(proj_round)
            surplus = kr - pr
            talent_weight = (22 - pr) ** 1.5
            keeper_value = round(surplus * talent_weight, 1) if surplus > 0 else 0
        except (ValueError, TypeError):
            keeper_value = ""

        rows.append({
            "Fantasy Team": p["team_name"],
            "Player": player_str,
            "2026 ADP": p["adp"],
            "2026 Proj Rnd": proj_round,
            "2025 Score": score_info.get("score", ""),
            "2025 Draft Rnd": str(draft["round"]) if draft else "",
            "2026 Keeper Rnd": keeper_rnd_2026,
            "2026 Keeper Cost": keeper_cost,
            "Keeper Value": keeper_value,
        })

    # Sort by team name, then ADP
    def sort_key(r):
        try:
            adp = float(r["2026 ADP"]) if r["2026 ADP"] else 9999
        except ValueError:
            adp = 9999
        return (r["Fantasy Team"], adp)

    rows.sort(key=sort_key)

    # Output
    fields = ["Fantasy Team", "Player", "2026 ADP", "2026 Proj Rnd", "2026 Keeper Rnd", "Keeper Value", "2025 Score", "2025 Draft Rnd", "2026 Keeper Cost"]

    if args.sheets:
        from datetime import datetime

        sheet_title = f"2026 Keepers ({datetime.now().strftime('%m-%d')})"

        # Find existing keepers sheet (any date) to overwrite, or create new
        existing = [ws for ws in spreadsheet.worksheets() if ws.title.startswith("2026 Keepers")]
        if existing:
            worksheet = existing[0]
            worksheet.clear()
            worksheet.update_title(sheet_title)
        else:
            worksheet = spreadsheet.add_worksheet(title=sheet_title, rows=len(rows) + 1, cols=len(fields))

        # Build all data at once: header + rows
        all_values = [fields]
        for r in rows:
            all_values.append([r[f] for f in fields])
        worksheet.update(all_values, value_input_option="USER_ENTERED")

        print(f"Wrote {len(rows)} rows to sheet '{sheet_title}'", file=sys.stderr)
        print(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={worksheet.id}")
    else:
        # Pretty table
        widths = {f: max(len(f), max((len(str(r[f])) for r in rows), default=0)) for f in fields}
        header = " | ".join(f.ljust(widths[f]) for f in fields)
        sep = "-+-".join("-" * widths[f] for f in fields)
        print(header)
        print(sep)
        current_team = None
        for r in rows:
            if r["Fantasy Team"] != current_team:
                if current_team is not None:
                    print(sep)
                current_team = r["Fantasy Team"]
            line = " | ".join(str(r[f]).ljust(widths[f]) for f in fields)
            print(line)


if __name__ == "__main__":
    main()
