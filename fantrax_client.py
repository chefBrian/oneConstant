"""Fantrax API client for H2H Categories leagues."""
from __future__ import annotations

import requests


API_URL = "https://www.fantrax.com/fxpa/req"


class FantraxClient:
    """Lightweight client that hits the Fantrax API directly."""

    def __init__(self, league_id: str):
        self.league_id = league_id
        self.session = requests.Session()
        self._team_map = None  # team_id -> team_name
        self._categories = None  # list of category dicts from header

    def _call(self, method: str, **kwargs) -> dict:
        data = {"leagueId": self.league_id}
        for k, v in kwargs.items():
            data[k] = str(v) if not isinstance(v, str) else v
        payload = {"msgs": [{"method": method, "data": data}]}
        resp = self.session.post(API_URL, params={"leagueId": self.league_id}, json=payload)
        resp.raise_for_status()
        return resp.json()["responses"][0]["data"]

    def _call_multi(self, methods: list[dict]) -> list[dict]:
        msgs = []
        for m in methods:
            name = m["method"]
            data = {"leagueId": self.league_id}
            for k, v in m.get("params", {}).items():
                data[k] = str(v) if not isinstance(v, str) else v
            msgs.append({"method": name, "data": data})
        resp = self.session.post(API_URL, params={"leagueId": self.league_id}, json={"msgs": msgs})
        resp.raise_for_status()
        return [r["data"] for r in resp.json()["responses"]]

    # --- Team info ---

    @property
    def team_map(self) -> dict[str, str]:
        if self._team_map is None:
            data = self._call("getStandings")
            self._team_map = {}
            for table in data["tableList"]:
                for row in table.get("rows", []):
                    fc = row["fixedCells"]
                    cell = fc[1] if len(fc) > 1 else fc[0]
                    if "teamId" in cell:
                        self._team_map[cell["teamId"]] = cell["content"]
            # Also populate from fantasyTeamInfo if present
            if "fantasyTeamInfo" in data:
                for tid, info in data["fantasyTeamInfo"].items():
                    if tid not in self._team_map:
                        self._team_map[tid] = info.get("name", tid)
        return self._team_map

    def team_name(self, team_id: str) -> str:
        return self.team_map.get(team_id, team_id)

    # --- Standings ---

    def standings(self, period: int | None = None) -> list[dict]:
        """Get standings, optionally through a specific period.

        Returns list of dicts with keys:
            rank, team_id, team_name, wins, losses, ties, win_pct,
            games_back, cat_points_for, cat_points_against
        """
        kwargs = {}
        if period is not None:
            kwargs = {"period": period, "timeframeType": "BY_PERIOD", "timeStartType": "FROM_SEASON_START"}
        data = self._call("getStandings", **kwargs)
        table = data["tableList"][0]
        header_keys = [c["key"] for c in table["header"]["cells"]]

        results = []
        for row in table["rows"]:
            fc = row["fixedCells"]
            rank = int(fc[0]["content"])
            team_cell = fc[1] if len(fc) > 1 else fc[0]
            team_id = team_cell["teamId"]
            team_name = team_cell["content"]

            cells = {header_keys[i]: row["cells"][i]["content"] for i in range(len(header_keys))}
            results.append({
                "rank": rank,
                "team_id": team_id,
                "team_name": team_name,
                "wins": int(cells.get("win", 0)),
                "losses": int(cells.get("loss", 0)),
                "ties": int(cells.get("tie", 0)),
                "win_pct": cells.get("winpc", ".000"),
                "games_back": cells.get("gb", "0"),
                "cat_points_for": float(cells.get("cpf", 0)),
                "cat_points_against": float(cells.get("cpa", 0)),
            })
        return results

    # --- Schedule / Matchup Results ---

    def schedule(self) -> list[dict]:
        """Get all scoring period matchup results.

        Returns list of period dicts, each with:
            period_num, name, date_range, matchups (list of matchup dicts)
        Each matchup dict has:
            away_team_id, away_team_name, home_team_id, home_team_name,
            away_record (W-L-T), home_record, away_cats {}, home_cats {},
            categories (list of category names)
        """
        data = self._call("getStandings", view="SCHEDULE")
        periods = []

        for table in data["tableList"]:
            caption = table.get("caption", "")
            sub = table.get("subCaption", "")
            header_cells = table["header"]["cells"]

            # First 4 columns: W, L, T, Pts — rest are stat categories
            cat_names = [c["shortName"] for c in header_cells[4:]]
            cat_full_names = [c["name"] for c in header_cells[4:]]

            rows = table["rows"]
            matchups = []
            # Rows come in pairs (away, home) sharing a matchupId
            i = 0
            while i < len(rows) - 1:
                away_row = rows[i]
                home_row = rows[i + 1]

                # Verify they share a matchup
                if away_row.get("matchupId") != home_row.get("matchupId"):
                    i += 1
                    continue

                away_cell = away_row["fixedCells"][0]
                home_cell = home_row["fixedCells"][0]

                away_cells = away_row["cells"]
                home_cells = home_row["cells"]

                # Skip matchups with no data (future periods)
                if not away_cells[0]["content"]:
                    i += 2
                    continue

                away_cats = {}
                home_cats = {}
                for j, cat in enumerate(cat_names):
                    idx = j + 4
                    key = cat_full_names[j]
                    away_cats[key] = {
                        "value": away_cells[idx]["content"],
                        "winning": away_cells[idx].get("gainColor", 0) == 1,
                    }
                    home_cats[key] = {
                        "value": home_cells[idx]["content"],
                        "winning": home_cells[idx].get("gainColor", 0) == 1,
                    }

                matchups.append({
                    "away_team_id": away_cell["teamId"],
                    "away_team_name": away_cell["content"],
                    "home_team_id": home_cell["teamId"],
                    "home_team_name": home_cell["content"],
                    "away_wins": int(away_cells[0]["content"]),
                    "away_losses": int(away_cells[1]["content"]),
                    "away_ties": int(away_cells[2]["content"]),
                    "away_points": float(away_cells[3]["content"]),
                    "home_wins": int(home_cells[0]["content"]),
                    "home_losses": int(home_cells[1]["content"]),
                    "home_ties": int(home_cells[2]["content"]),
                    "home_points": float(home_cells[3]["content"]),
                    "away_cats": away_cats,
                    "home_cats": home_cats,
                    "categories": cat_full_names,
                })
                i += 2

            periods.append({
                "period_num": len(periods) + 1,
                "name": caption,
                "date_range": sub,
                "matchups": matchups,
            })

        return periods

    def period_results(self, period_num: int) -> dict | None:
        """Get results for a specific scoring period."""
        schedule = self.schedule()
        for p in schedule:
            if p["period_num"] == period_num:
                return p
        return None

    def latest_completed_period(self) -> dict | None:
        """Get the most recent completed scoring period.

        We figure this out by checking which periods have non-zero matchup scores.
        The last one with actual scores is the most recently completed.
        """
        schedule = self.schedule()
        latest = None
        for p in schedule:
            if not p["matchups"]:
                continue
            # Check if matchups have actual scores
            m = p["matchups"][0]
            if m["away_wins"] + m["away_losses"] + m["away_ties"] > 0:
                latest = p
        return latest

    # --- Transactions ---

    def transactions(self, count: int = 50) -> list[dict]:
        """Get recent transactions."""
        data = self._call("getTransactionDetailsHistory", maxResultsPerPage=count)
        if "table" not in data or "rows" not in data["table"]:
            return []

        txns = []
        for row in data["table"]["rows"]:
            scorer = row.get("scorer", {})
            cells = row.get("cells", [])
            team_name = cells[0]["content"] if cells else "Unknown"
            date_str = cells[3]["content"] if len(cells) > 3 else ""
            txns.append({
                "player_name": scorer.get("name", "Unknown"),
                "team_name": team_name,
                "type": row.get("transactionType", "Unknown"),
                "claim_type": row.get("claimType", ""),
                "date": date_str,
            })
        return txns


if __name__ == "__main__":
    client = FantraxClient("uo0es7lom23shg6b")

    print("Teams:", list(client.team_map.values()))
    print()

    standings = client.standings()
    print("STANDINGS:")
    for s in standings:
        print(f"  {s['rank']}. {s['team_name']} ({s['wins']}-{s['losses']}-{s['ties']}) {s['win_pct']}")
    print()

    latest = client.latest_completed_period()
    if latest:
        print(f"LATEST PERIOD: {latest['name']} {latest['date_range']}")
        for m in latest["matchups"]:
            winner = m["away_team_name"] if m["away_wins"] > m["home_wins"] else m["home_team_name"]
            print(f"  {m['away_team_name']} ({m['away_wins']}-{m['away_losses']}-{m['away_ties']}) vs {m['home_team_name']} ({m['home_wins']}-{m['home_losses']}-{m['home_ties']}) -> {winner}")
