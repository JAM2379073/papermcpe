#!/bin/bash
set -e

echo "🛑 Stopping all previous instances..."

# Stop all playit containers
docker ps -a --filter "name=playit" --format "{{.ID}}" | xargs -r docker stop 2>/dev/null || true
docker ps -a --filter "name=playit" --format "{{.ID}}" | xargs -r docker rm 2>/dev/null || true
echo "✅ Docker containers stopped"

# Cancel previous workflow runs
RUNS=$(curl -s \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Authorization: token ${GH_PAT}" \
  "https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/runs?status=in_progress" \
  | jq -r ".workflow_runs[] | select(.id != ${CURRENT_RUN_ID}) | .id")

for RUN_ID in $RUNS; do
  echo "Cancelling run: $RUN_ID"
  curl -s -X POST \
    -H "Accept: application/vnd.github.v3+json" \
    -H "Authorization: token ${GH_PAT}" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/runs/${RUN_ID}/cancel"
done

echo "⏳ Waiting 45s for clean shutdown..."
sleep 45
echo "✅ All previous instances terminated"
