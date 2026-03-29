#!/bin/bash
set -e

echo "🔄 Triggering next workflow run..."

curl -X POST \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Authorization: token ${GH_PAT}" \
  "https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/workflows/minecraft.yml/dispatches" \
  -d '{"ref":"main"}'

echo "✅ Next workflow triggered!"
