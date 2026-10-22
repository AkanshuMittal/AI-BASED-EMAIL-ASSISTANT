#!/usr/bin/env bash
# First deployment. Run in Cloud Shell from the repo root, after setup_gcp.sh:
#   export PROJECT_ID=your-project-id
#   bash deploy/first_deploy.sh            (or: USE_CLOUD_BUILD=1 bash deploy/first_deploy.sh)
set -euo pipefail

: "${PROJECT_ID:?export PROJECT_ID=...}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-ai-based-email-agent}"   # becomes the start of the public URL
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/quill/quill:first"
RUN_SA="quill-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
# If `docker push` fails from Cloud Shell (network error), run with USE_CLOUD_BUILD=1:
# the image is then built and pushed by Google's servers instead.
if [ "${USE_CLOUD_BUILD:-0}" = "1" ]; then
  gcloud services enable cloudbuild.googleapis.com
  gcloud builds submit --tag "$IMAGE" .
else
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
  docker build -t "$IMAGE" .
  pushed=0
  for attempt in 1 2 3 4 5; do
    if docker push "$IMAGE"; then pushed=1; break; fi
    echo "docker push failed (attempt $attempt of 5), retrying in 5s..."
    sleep 5
  done
  [ "$pushed" = "1" ] || { echo "docker push kept failing. Re-run with USE_CLOUD_BUILD=1"; exit 1; }
fi

# scale-to-zero (no idle cost), capped at 2 instances, websocket-friendly timeout
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" --region "$REGION" --allow-unauthenticated \
  --service-account "$RUN_SA" \
  --set-secrets "GROQ_API_KEY=groq-api-key:latest,GOOGLE_CLIENT_ID=google-client-id:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest" \
  --set-env-vars "GOOGLE_REDIRECT_URI=https://example.invalid,LANGSMITH_TRACING=false" \
  --min-instances 0 --max-instances 2 --memory 1Gi --timeout 3600 --session-affinity

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "GOOGLE_REDIRECT_URI=${URL}"

echo
echo "Live at: ${URL}"
echo "Now add this EXACT URL (no trailing slash) to Google Console > Clients > Authorized redirect URIs."