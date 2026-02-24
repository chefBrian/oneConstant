"""Firestore state management for transaction watcher.

Uses a subcollection pattern - each seen transaction ID is its own document,
avoiding the 1 MiB document size limit and making reads cheaper.

Data model:
    Collection: leagues
      Document: {league_id}
        Subcollection: seen_ids
          Document: {tx_set_id}
            Fields: created_at (server timestamp)

Auth priority:
    1. FIREBASE_SERVICE_ACCOUNT_BASE64 env var (Cloud Functions + CI)
    2. GOOGLE_APPLICATION_CREDENTIALS env var (local dev)
    3. Application Default Credentials (gcloud auth application-default login)
"""
import base64
import json
import os

import firebase_admin
from firebase_admin import credentials, firestore

_app = None


def _get_db():
    """Initialize Firebase app (once) and return Firestore client."""
    global _app
    if _app is None:
        b64 = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64")
        options = {}
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project_id:
            options["projectId"] = project_id
        if b64:
            info = json.loads(base64.b64decode(b64))
            cred = credentials.Certificate(info)
        elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        else:
            cred = credentials.ApplicationDefault()
        _app = firebase_admin.initialize_app(cred, options=options)
    return firestore.client()


def _seen_ids_collection(league_id: str):
    """Return a reference to the seen_ids subcollection for a league."""
    db = _get_db()
    return db.collection("leagues").document(league_id).collection("seen_ids")


def has_any_seen_ids(league_id: str) -> bool:
    """Check if any seen IDs exist for this league (first-run detection)."""
    col = _seen_ids_collection(league_id)
    docs = col.limit(1).get()
    return len(docs) > 0


def load_seen_ids(league_id: str, tx_set_ids: list[str]) -> set[str]:
    """Check which of the given IDs already exist in Firestore.

    Uses individual document reads (batched by caller's list).
    Returns the subset of tx_set_ids that are already seen.
    """
    if not tx_set_ids:
        return set()

    col = _seen_ids_collection(league_id)
    seen = set()
    for tx_id in tx_set_ids:
        doc = col.document(tx_id).get()
        if doc.exists:
            seen.add(tx_id)
    return seen


def save_seen_ids(league_id: str, new_ids: list[str]) -> None:
    """Write newly seen IDs to Firestore (batch write)."""
    if not new_ids:
        return

    db = _get_db()
    col = _seen_ids_collection(league_id)
    batch = db.batch()
    for tx_id in new_ids:
        batch.set(col.document(tx_id), {"created_at": firestore.SERVER_TIMESTAMP})
    batch.commit()


def seed_seen_ids(league_id: str, all_ids: list[str]) -> None:
    """Bulk-write all IDs on first run (chunked into 500-op batches)."""
    if not all_ids:
        return

    db = _get_db()
    col = _seen_ids_collection(league_id)

    for i in range(0, len(all_ids), 500):
        chunk = all_ids[i : i + 500]
        batch = db.batch()
        for tx_id in chunk:
            batch.set(col.document(tx_id), {"created_at": firestore.SERVER_TIMESTAMP})
        batch.commit()
