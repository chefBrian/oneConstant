"""Cloud Functions entry points.

Deployed as 2nd gen HTTP functions, triggered by Cloud Scheduler.
Env vars (set via gcloud deploy): FANTRAX_LEAGUE_ID, DISCORD_WEBHOOK_URL,
DISCORD_TRANSACTION_WEBHOOK_URL, GOOGLE_CLOUD_PROJECT.
"""
import os

import functions_framework

from transaction_watcher import check_once
from bot import run_recap


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


@functions_framework.http
def weekly_recap(request):
    league_id = os.environ.get("FANTRAX_LEAGUE_ID")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not league_id or not webhook_url:
        return "Missing FANTRAX_LEAGUE_ID or DISCORD_WEBHOOK_URL", 500

    # Allow overriding period via query param (e.g. ?period=10)
    period = request.args.get("period", type=int)

    try:
        run_recap(league_id, webhook_url, period=period)
        return "OK", 200
    except Exception as e:
        print(f"Error: {e}")
        return f"Error: {e}", 500
