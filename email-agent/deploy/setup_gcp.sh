#!/usr/bin/env bash
# One-time Google Cloud setup. Run in Google Cloud Shell:
#   export PROJECT_ID=your-project-id
#   export GITHUB_REPO=your-github-username/your-repo
#   bash deploy/setup_gcp.sh
set -euo pipefail

: "${PROJECT_ID:?export PROJECT_ID=...}"
: "${GITHUB_REPO:?export GITHUB_REPO=username/repo}"
REGION="${REGION:-asia-south1}"

gcloud config set project "$PROJECT_ID"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUN_SA="quill-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOY_SA="gh-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iamcredentials.googleapis.com

echo "==> Artifact Registry repo"
gcloud artifacts repositories describe quill --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create quill --repository-format=docker --location "$REGION"

echo "==> Service accounts"
gcloud iam service-accounts describe "$RUN_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create quill-runtime --display-name="Quill runtime"
gcloud iam service-accounts describe "$DEPLOY_SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create gh-deployer --display-name="GitHub deployer"
sleep 15  # let IAM propagate

echo "==> Permissions for the GitHub deployer"
for ROLE in roles/run.admin roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$ROLE" --condition=None >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/iam.serviceAccountUser" >/dev/null

echo "==> Secrets (typed values are hidden and never saved in shell history)"
make_secret () {
  local name="$1" label="$2" value
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    echo "Secret $name already exists, keeping it."
  else
    read -r -s -p "$label: " value; echo
    printf "%s" "$value" | gcloud secrets create "$name" --data-file=-
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:${RUN_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
}
make_secret groq-api-key "Groq API key"
make_secret google-client-id "Google OAuth client ID"
make_secret google-client-secret "Google OAuth client secret"

echo "==> Keyless GitHub -> Google auth (Workload Identity Federation)"
gcloud iam workload-identity-pools describe github-pool --location=global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create github-pool --location=global --display-name="GitHub"
gcloud iam workload-identity-pools providers describe github-provider \
  --location=global --workload-identity-pool=github-pool >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc github-provider \
    --location=global --workload-identity-pool=github-pool \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}'"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${GITHUB_REPO}" >/dev/null

echo
echo "Done. Add these as GitHub repository VARIABLES (Settings > Secrets and variables > Actions > Variables):"
echo "  GCP_PROJECT_ID   = ${PROJECT_ID}"
echo "  GCP_REGION       = ${REGION}"
echo "  GCP_DEPLOY_SA    = ${DEPLOY_SA}"
echo "  GCP_WIF_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/providers/github-provider"