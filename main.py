"""Cloud Functions entry points.

Deployed as 2nd gen HTTP functions, triggered by Cloud Scheduler.
Env vars (set via gcloud deploy): FANTRAX_LEAGUE_ID, DISCORD_WEBHOOK_URL,
DISCORD_TRANSACTION_WEBHOOK_URL, GOOGLE_CLOUD_PROJECT, SCHEDULER_SECRET.
"""
import hashlib
import hmac
import os

import functions_framework

from reports.power_rankings import run_power_rankings
from services.transaction_watcher import check_once
from services.weekly_recap import run_recap


def _verify_scheduler(request) -> bool:
    """Verify the request came from Cloud Scheduler via a shared secret header."""
    secret = os.environ.get("SCHEDULER_SECRET")
    if not secret:
        # No secret configured - allow (for backwards compat during rollout)
        return True
    token = request.headers.get("X-Scheduler-Secret", "")
    return hmac.compare_digest(token, secret)


@functions_framework.http
def watch_transactions(request):
    if not _verify_scheduler(request):
        return "Unauthorized", 403

    league_id = os.environ.get("FANTRAX_LEAGUE_ID")
    webhook_url = os.environ.get("DISCORD_TRANSACTION_WEBHOOK_URL")

    if not league_id or not webhook_url:
        return "Missing FANTRAX_LEAGUE_ID or DISCORD_TRANSACTION_WEBHOOK_URL", 500

    try:
        check_once(league_id, webhook_url, dry_run=False)
        return "OK", 200
    except Exception as e:
        print(f"Error: {e}")
        return "Internal error", 500


@functions_framework.http
def weekly_recap(request):
    if not _verify_scheduler(request):
        return "Unauthorized", 403

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
        return "Internal error", 500


@functions_framework.http
def power_rankings(request):
    """Post power rankings every 4 weeks (after periods 4, 8, 12, 16, 20).

    Cloud Scheduler should invoke this on the same Monday cadence as
    weekly_recap; the function short-circuits unless the latest completed
    period is a multiple of 4.
    """
    if not _verify_scheduler(request):
        return "Unauthorized", 403

    league_id = os.environ.get("FANTRAX_LEAGUE_ID")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not league_id or not webhook_url:
        return "Missing FANTRAX_LEAGUE_ID or DISCORD_WEBHOOK_URL", 500

    force = request.args.get("force", "").lower() in ("1", "true", "yes")

    try:
        status = run_power_rankings(league_id, webhook_url, force=force)
        return status, 200
    except Exception as e:
        print(f"Error: {e}")
        return "Internal error", 500
