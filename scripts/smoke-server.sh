#!/bin/sh
set -eu

BASE_URL="${SMOKE_BASE_URL:-}"
USERNAME="${FRC_SMOKE_USERNAME:-admin}"
PASSWORD="${FRC_SMOKE_PASSWORD:-}"
INSECURE="${SMOKE_INSECURE:-false}"

if [ -z "$BASE_URL" ] || [ -z "$PASSWORD" ]; then
  echo "Usage: SMOKE_BASE_URL=https://host FRC_SMOKE_PASSWORD=... $0" >&2
  exit 2
fi

COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

CURL_TLS=""
if [ "$INSECURE" = "true" ]; then
  CURL_TLS="--insecure"
fi

status="$(curl $CURL_TLS -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/health/live")"
[ "$status" = "200" ] || { echo "FAIL health/live HTTP $status" >&2; exit 1; }

status="$(curl $CURL_TLS -sS -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/dashboard")"
[ "$status" = "401" ] || { echo "FAIL unauthenticated dashboard HTTP $status" >&2; exit 1; }

LOGIN_PAYLOAD="$(FRC_SMOKE_USERNAME="$USERNAME" FRC_SMOKE_PASSWORD="$PASSWORD" python3 -c 'import json, os; print(json.dumps({"username": os.environ["FRC_SMOKE_USERNAME"], "password": os.environ["FRC_SMOKE_PASSWORD"]}))')"
status="$(curl $CURL_TLS -sS -c "$COOKIE_JAR" -H 'Content-Type: application/json' -d "$LOGIN_PAYLOAD" -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/auth/login")"
[ "$status" = "200" ] || { echo "FAIL login HTTP $status" >&2; exit 1; }

ME_RESPONSE="$(curl $CURL_TLS -sS -b "$COOKIE_JAR" "$BASE_URL/api/v1/auth/me")"
printf '%s' "$ME_RESPONSE" | FRC_SMOKE_USERNAME="$USERNAME" python3 -c 'import json, os, sys; data=json.load(sys.stdin); assert data["username"] == os.environ["FRC_SMOKE_USERNAME"]'

DASHBOARD_RESPONSE="$(curl $CURL_TLS -sS -b "$COOKIE_JAR" "$BASE_URL/api/v1/dashboard")"
printf '%s' "$DASHBOARD_RESPONSE" | python3 -c 'import json, sys; data=json.load(sys.stdin); assert "mode" in data and "health" in data and "positions" in data'

INTEGRATIONS_RESPONSE="$(curl $CURL_TLS -sS -b "$COOKIE_JAR" "$BASE_URL/api/v1/integrations")"
printf '%s' "$INTEGRATIONS_RESPONSE" | python3 -c 'import json, sys; data=json.load(sys.stdin); assert data["binance"]["environment"] in {"testnet", "live"}; assert "model" in data'

echo "PASS server smoke: health, auth boundary, login, session, dashboard, integrations"
