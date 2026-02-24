"""Cloud Functions entry point for transaction watcher.

Deployed as a 2nd gen HTTP function, triggered by Cloud Scheduler every minute.
Env vars (set via gcloud deploy): FANTRAX_LEAGUE_ID, DISCORD_TRANSACTION_WEBHOOK_URL,
FIREBASE_SERVICE_ACCOUNT_BASE64.
"""
import os

import functions_framework

from transaction_watcher import check_once


@functions_framework.http
def watch_transactions(request):
    league_id = os.environ.get("FANTRAX_LEAGUE_ID")
    webhook_url = os.environ.get("DISCORD_TRANSACTION_WEBHOOK_URL")

    if not league_id or not webhook_url:
        return "Missing FANTRAX_LEAGUE_ID or DISCORD_TRANSACTION_WEBHOOK_URL", 500

    try:
        check_once(league_id, webhook_url, dry_run=False)
        return "OK", 200
    except Exception as e:
        print(f"Error: {e}")
        return f"Error: {e}", 500
