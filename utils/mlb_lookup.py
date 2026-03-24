"""Look up MLB player IDs from the Stats API."""

import requests

MLB_SEARCH_URL = "https://statsapi.mlb.com/api/v1/people/search"


def lookup_mlb_id(player_name: str) -> int | None:
    """Look up a player's MLB ID by name. Returns None if not found."""
    try:
        resp = requests.get(MLB_SEARCH_URL, params={"names": player_name}, timeout=5)
        resp.raise_for_status()
        people = resp.json().get("people", [])
        if people:
            return people[0]["id"]
    except (requests.RequestException, KeyError, IndexError):
        pass
    return None


def enrich_mlb_ids(txn: dict) -> None:
    """Add mlb_id to added/dropped players in a transaction dict."""
    for key in ("added", "dropped"):
        player = txn.get(key)
        if player and not player.get("mlb_id"):
            player["mlb_id"] = lookup_mlb_id(player["name"])
