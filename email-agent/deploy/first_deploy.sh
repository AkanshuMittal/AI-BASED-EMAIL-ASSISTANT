#!/usr/bin/env bash
# First deployment. Run in Cloud Shell from the repo root, after setup_gcp.sh:
#   export PROJECT_ID=your-project-id
#   bash deploy/first_deploy.sh
set -euo pipefail

: "${PROJECT_ID:?export PROJECT_ID=...}"
REGION="${REGION:-asia-south1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/quill/quill:first"
RUN_SA="quill-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

docker build -t "$IMAGE" .
docker push "$IMAGE"

# scale-to-zero (no idle cost), capped at 2 instances, websocket-friendly timeout
gcloud run deploy quill \
  --image "$IMAGE" --region "$REGION" --allow-unauthenticated \
  --service-account "$RUN_SA" \
  --set-secrets "GROQ_API_KEY=groq-api-key:latest,GOOGLE_CLIENT_ID=google-client-id:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest" \
  --set-env-vars "GOOGLE_REDIRECT_URI=https://example.invalid,LANGSMITH_TRACING=false" \
  --min-instances 0 --max-instances 2 --memory 1Gi --timeout 3600 --session-affinity

URL="$(gcloud run services describe quill --region "$REGION" --format='value(status.url)')"
gcloud run services update quill --region "$REGION" --update-env-vars "GOOGLE_REDIRECT_URI=${URL}"

echo
echo "Live at: ${URL}"
echo "Now add this EXACT URL (no trailing slash) to Google Console > Clients > Authorized redirect URIs."