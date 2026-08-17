#!/usr/bin/env bash
# Seed the redirect table and test the flow.
#
# This is a bash script. On Windows, run it under WSL or Git Bash, or run the
# equivalent `aws dynamodb put-item` and `curl` commands directly in PowerShell.
#
# Phase 1 (default *.cloudfront.net domain): test with an injected x-vanity-host header.
#   ./seed_and_test.sh <DistributionDomain>
#
# Phase 2 (real tenant domain): test the domain directly.
#   ./seed_and_test.sh <tenant-fqdn> direct
#
# Region defaults to us-east-1 (required for CloudFront/Lambda@Edge).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
TABLE="${TABLE_NAME:-vanity-redirect-sample}"
HOST_UNDER_TEST="${1:?pass the distribution domain (phase 1) or tenant FQDN (phase 2)}"
MODE="${2:-header}"   # 'header' = inject x-vanity-host (phase 1); 'direct' = real domain (phase 2)

VANITY_HOST="${VANITY_HOST:-vanity.example.com}"

echo "==> Seeding $TABLE (region $REGION)"
aws dynamodb put-item --region "$REGION" --table-name "$TABLE" --item "{
  \"host\": {\"S\": \"$VANITY_HOST\"},
  \"targetUrl\": {\"S\": \"https://aws.amazon.com/\"},
  \"status\": {\"S\": \"active\"}
}"
aws dynamodb put-item --region "$REGION" --table-name "$TABLE" --item "{
  \"host\": {\"S\": \"disabled.example.com\"},
  \"targetUrl\": {\"S\": \"https://aws.amazon.com/\"},
  \"status\": {\"S\": \"disabled\"}
}"

if [ "$MODE" = "direct" ]; then
  BASE="https://$HOST_UNDER_TEST"; HDR=()
else
  BASE="https://$HOST_UNDER_TEST"; HDR=(-H "x-vanity-host: $VANITY_HOST")
fi

echo; echo "==> Test 1: active host -> expect 302 + Location + HSTS"
curl -s -o /dev/null -D - "${HDR[@]}" "$BASE/?cb=$RANDOM" | grep -iE 'HTTP/|^location:|^strict-transport'

echo; echo "==> Test 2: disabled host -> expect 403"
curl -s -o /dev/null -w "status=%{http_code}\n" -H "x-vanity-host: disabled.example.com" "$BASE/?cb=$RANDOM"

echo; echo "==> Test 3: unknown host -> expect 403"
curl -s -o /dev/null -w "status=%{http_code}\n" -H "x-vanity-host: nope.example.com" "$BASE/?cb=$RANDOM"
