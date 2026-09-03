#!/usr/bin/env bash
# Deploy anki-mcp to Cloud Run, sourcing secrets from .env.
# --no-cpu-throttling keeps the CPU on between requests so the idle sync flush runs.
#
#   scripts/deploy.sh <gcp-project> [region]
#
# First run: creates the four secrets in Secret Manager and deploys with a
# placeholder BASE_URL, then prints the service URL. Put that URL in .env as
# BASE_URL and run again; the second deploy sets the real BASE_URL.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${1:?usage: scripts/deploy.sh <gcp-project> [region]}"
REGION="${2:-us-central1}"
SERVICE=anki-mcp

set -a; source .env; set +a
for v in ANKIWEB_USERNAME ANKIWEB_PASSWORD OWNER_PASSWORD JWT_SIGNING_KEY; do
  [[ -n "${!v:-}" ]] || { echo "$v is empty in .env" >&2; exit 1; }
done

gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT"

# Newly enabled APIs take a little while to propagate.
for i in $(seq 1 24); do
  gcloud secrets list --project "$PROJECT" --limit 1 >/dev/null 2>&1 && break
  echo "waiting for Secret Manager API to become available ($i)..."; sleep 5
done

upsert_secret() { # name value
  if gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --project "$PROJECT" --data-file=- >/dev/null
  else
    printf '%s' "$2" | gcloud secrets create "$1" --project "$PROJECT" --replication-policy=automatic --data-file=- >/dev/null
  fi
  echo "secret $1: ok"
}
upsert_secret ankiweb-username "$ANKIWEB_USERNAME"
upsert_secret ankiweb-password "$ANKIWEB_PASSWORD"
upsert_secret owner-password   "$OWNER_PASSWORD"
upsert_secret jwt-signing-key  "$JWT_SIGNING_KEY"

# Let the Cloud Run runtime service account read the secrets.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for s in ankiweb-username ankiweb-password owner-password jwt-signing-key; do
  gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

# New projects do not grant the default compute SA build permissions; --source deploys need them.
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
  --role=roles/cloudbuild.builds.builder --condition=None >/dev/null
echo "build permissions: ok"

BASE="${BASE_URL:-http://placeholder}"
gcloud run deploy "$SERVICE" --source . --project "$PROJECT" --region "$REGION" --quiet \
  --allow-unauthenticated --min-instances 0 --max-instances 1 --concurrency 4 \
  --memory 512Mi --cpu 1 --timeout 300 --no-cpu-throttling \
  --set-env-vars "BASE_URL=$BASE,COLLECTION_DIR=/tmp/anki-mcp" \
  --set-secrets "ANKIWEB_USERNAME=ankiweb-username:latest,ANKIWEB_PASSWORD=ankiweb-password:latest,OWNER_PASSWORD=owner-password:latest,JWT_SIGNING_KEY=jwt-signing-key:latest"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')
echo
if [[ "$BASE" != http://placeholder ]] && curl -fsS "$BASE/health" >/dev/null 2>&1; then
  echo "healthy at $BASE"
  echo "connector URL for claude.ai: $BASE/mcp"
else
  echo "service URL: $URL   (Cloud Run also serves a deterministic *.run.app URL; either works)"
  echo "Set BASE_URL in .env to the URL you want claude.ai to use and run this script again."
fi
