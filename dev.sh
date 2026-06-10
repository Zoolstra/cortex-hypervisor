#!/usr/bin/env bash
#
# dev.sh — build, push, and redeploy cortex-hypervisor to Cloud Run.
#
# No arguments. Run from anywhere:
#     ./dev.sh
#
# Prerequisites (one-time):
#   - gcloud auth login                       (deploy identity)
#   - gcloud auth configure-docker us-docker.pkg.dev   (push to Artifact Registry)
#
# The DB schema is migrated separately (alembic upgrade head) and is
# backward-compatible, so this script only ships the container image.
set -euo pipefail

# ── Production coordinates ──────────────────────────────────────────────────
PROJECT="project-demo-2-482101"
REGION="us-central1"
SERVICE="cortex-hypervisor"
REPO="us-docker.pkg.dev/${PROJECT}/cortex-hypervisor/cortex-hypervisor"
IMAGE="${REPO}:latest"

# Build from this script's own directory so cwd doesn't matter.
cd "$(dirname "$0")"

echo "▶ Building ${IMAGE}"
docker build -t "${IMAGE}" .

echo "▶ Pushing ${IMAGE}"
docker push "${IMAGE}"

# Resolve the digest we just pushed and deploy BY DIGEST, not by the :latest
# tag. Cloud Run pins a revision to an image digest; deploying with the tag
# string (unchanged across builds) makes `services update` a no-op when only
# the image *contents* changed — so the service keeps serving the old build
# until you force it in the console. Deploying the digest guarantees a fresh
# revision on every content change.
echo "▶ Resolving pushed digest"
DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "${IMAGE}" | cut -d'@' -f2)"
if [[ -z "${DIGEST}" ]]; then
  echo "✗ Could not resolve image digest after push (RepoDigests empty)." >&2
  exit 1
fi
echo "  ${REPO}@${DIGEST}"

echo "▶ Deploying to Cloud Run service '${SERVICE}' (${PROJECT}/${REGION})"
gcloud run services update "${SERVICE}" \
  --project="${PROJECT}" \
  --region="${REGION}" \
  --image="${REPO}@${DIGEST}"

echo "✓ Deployed. URL: https://cortex-hypervisor-45007506504.us-central1.run.app"
