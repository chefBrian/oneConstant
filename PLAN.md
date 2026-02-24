# Plan: Transaction Watcher on Cloud Functions + Cloud Scheduler

## Context

`transaction_watcher.py` currently stores seen transaction IDs in a local `seen_transactions.json` file and runs as a long-running polling process. Goal: run it every minute using GCP, targeting ~$1/month (Firestore read overages).

## Why not GitHub Actions?

- GH Actions cron minimum is 5 minutes, and actual execution is often delayed 5-30+ minutes
- Running every minute would burn ~43,200 minutes/month - exceeds the 2,000 free minutes
- Not suitable for near-real-time transaction watching

## Architecture

```
Cloud Scheduler (every 1 min)
  → HTTP POST → Cloud Function (2nd gen)
      → Fantrax API (fetch transactions)
      → Firestore (check/write seen_ids)
      → Discord webhook (post new transactions)
      → return 200
```

### Cost estimate

| Service | Free Tier | Our Usage (1-min intervals) | Cost |
|---------|-----------|---------------------------|------|
| Cloud Functions | 2M invocations/mo | ~43,200/mo | $0 (within free tier) |
| Cloud Functions compute | 400K GB-s/mo | ~4,320 GB-s (256MB, ~6s each) | $0 (within free tier) |
| Cloud Scheduler | 3 jobs/account | 1 job | $0 (within free tier) |
| Firestore reads | 50K/day free | ~43,200/day (1 per invocation, only checking current batch) | $0 (within free tier) |
| Firestore writes | 20K/day free | ~50/day (only on changes) | $0 |

**Total: ~$0/month** (fully within free tier)

Note: Each invocation fetches transactions from Fantrax, then checks those specific IDs against Firestore. Most invocations find 0 new transactions and write nothing. Reads scale with the number of IDs in the current API batch (~50-70), not with history size.

## Approach

### 1. Create `firestore_client.py` - Firestore state wrapper

Thin module using a **subcollection pattern** - each seen ID is its own document, avoiding the 1 MiB document size limit and making reads cheaper (we only check IDs from the current batch, not the full history).

**Firestore data model:**
```
Collection: leagues
  Document: {FANTRAX_LEAGUE_ID}
    Subcollection: seen_ids
      Document: {tx_set_id}
        Fields:
          created_at: <server timestamp>
```

**API:**
- `load_seen_ids(league_id, tx_set_ids)` - check which of the given IDs already exist (batch of individual doc reads)
- `save_seen_ids(league_id, new_ids)` - write only newly seen IDs (batch write)
- `seed_seen_ids(league_id, all_ids)` - bulk-write on first run (chunked into 500-op batches)
- `has_any_seen_ids(league_id)` - first-run detection (single query, limit 1)

**Auth:** Uses `firebase-admin` SDK:
- `FIREBASE_SERVICE_ACCOUNT_BASE64` env var (base64-encoded service account JSON) for Cloud Functions + CI
- `GOOGLE_APPLICATION_CREDENTIALS` fallback for local dev (point to a service account JSON file)
- Application Default Credentials as final fallback (`gcloud auth application-default login`)

### 2. Update `transaction_watcher.py`

- Remove local `STATE_FILE`, `load_state()`, `save_state()` definitions
- Import from `firestore_client` instead
- Move `load_dotenv()` from module level into `main()` so it only runs in CLI context, not when imported by Cloud Functions
- Extract `check_once(league_id, webhook_url, dry_run)` - self-contained function that handles Firestore reads, Fantrax API calls, Discord posting, and Firestore writes. This is what both `main.py` and `--once` call.
- Add `--once` flag: calls `check_once()` and exits
- Keep existing `--interval` loop for local dev
- Fix partial-failure behavior: collect new IDs, attempt all Discord posts, only save IDs for transactions that were successfully posted (or all if `--dry-run`)

### 3. Create `main.py` - Cloud Functions entry point

Thin HTTP function that:
- Reads env vars (`FANTRAX_LEAGUE_ID`, `DISCORD_TRANSACTION_WEBHOOK_URL`)
- Calls `check_once()` from `transaction_watcher`
- Returns 200 on success, 500 on error with error message in response body
- Does NOT call `load_dotenv()` (env vars come from Cloud Functions runtime)
- Does NOT import `python-dotenv` (not needed in Cloud Functions)

```python
import functions_framework

@functions_framework.http
def watch_transactions(request):
    ...
```

### 4. Update `requirements.txt`

Add:
- `firebase-admin>=6.0.0`
- `functions-framework>=3.0.0`

### 5. Update config files

- `.gitignore`: add `seen_transactions.json` and `*-service-account*.json`
- `env-example.txt`: add `FIREBASE_SERVICE_ACCOUNT_BASE64` and `GOOGLE_APPLICATION_CREDENTIALS`

### 6. Deployment

No deploy scripts in repo - just documented `gcloud` commands:

```bash
# Deploy function
gcloud functions deploy transaction-watcher \
  --gen2 \
  --runtime python313 \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory 256Mi \
  --timeout 30s \
  --set-env-vars FANTRAX_LEAGUE_ID=xxx,DISCORD_TRANSACTION_WEBHOOK_URL=xxx,FIREBASE_SERVICE_ACCOUNT_BASE64=xxx \
  --region us-east1

# Create scheduler job (every 1 min)
gcloud scheduler jobs create http transaction-watcher-trigger \
  --schedule "* * * * *" \
  --uri "FUNCTION_URL" \
  --http-method POST \
  --oidc-service-account-email PROJECT_ID@appspot.gserviceaccount.com \
  --location us-east1
```

Note: 256MB minimum recommended. `firebase-admin` pulls in `grpcio`, `google-cloud-firestore`, and `google-auth` which are heavy - 128MB risks OOM on cold starts.

## Files to modify
- `transaction_watcher.py` - replace state layer, extract `check_once()`, move `load_dotenv()` into `main()`, add `--once`
- `requirements.txt` - add `firebase-admin`, `functions-framework`
- `.gitignore` - add service account + state file patterns
- `env-example.txt` - document new env vars

## Files to create
- `firestore_client.py` - Firestore wrapper (subcollection pattern)
- `main.py` - Cloud Functions entry point

## Manual setup needed after code changes
1. Create GCP project (or use existing Firebase project)
2. Enable Cloud Functions, Cloud Scheduler, Firestore APIs
3. Create Firestore database (Native mode)
4. Create service account with `Cloud Datastore User` role
5. Base64-encode the service account JSON
6. For local dev: set `GOOGLE_APPLICATION_CREDENTIALS` to the JSON file path, or run `gcloud auth application-default login`
7. Deploy function and scheduler job (commands above)
8. Run `python transaction_watcher.py --once` locally to seed Firestore with existing transactions

## Verification
- `python transaction_watcher.py --once --dry-run` to test Firestore read/write locally (requires Firestore auth set up)
- Check Firestore console to confirm `leagues/{league_id}/seen_ids` subcollection exists
- `gcloud functions call transaction-watcher` to test the deployed function
- Check Cloud Scheduler logs for successful invocations
- Verify cold start completes within 30s timeout (check Cloud Functions logs)

## Known limitations / future considerations
- **Race condition**: If an invocation takes >60s, two could overlap and duplicate Discord posts. Mitigated by the 30s timeout - if Fantrax is slow, the function fails rather than overlapping. If this becomes a problem, add a Firestore-based distributed lock.
- **Subcollection growth**: Seen IDs accumulate indefinitely. Not a practical concern for fantasy baseball transaction volumes, but could add a TTL cleanup Cloud Function if needed years from now.
