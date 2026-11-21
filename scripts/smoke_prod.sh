#!/usr/bin/env bash
# Smoke test of a deployed stack (deploy/): HTTPS, security headers, the public surface.
#
#   scripts/smoke_prod.sh lab-demo.duckdns.org
#   SMOKE_INSECURE=1 scripts/smoke_prod.sh localhost:8443   # local test, Caddy's internal CA
#
# Read-only except one unsigned POST to the webhook (rejected, nothing stored). Exit 1 on any
# failed check.
set -uo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
    echo "usage: $0 DOMAIN[:port]" >&2
    exit 2
fi
HOST="${1#https://}"
HOST="${HOST%/}"
BASE="https://${HOST}"
CURL=(curl -sS --max-time 15 --proto '=https')
[[ "${SMOKE_INSECURE:-0}" == "1" ]] && CURL+=(-k)

fails=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

# status URL [curl args...] -> HTTP status code
status() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || echo 000; }
# headers URL -> response headers, lower-cased names
headers() { "${CURL[@]}" -o /dev/null -D - "$1" 2>/dev/null | tr -d '\r' | awk -F': ' '{ $1 = tolower($1); print }' OFS=': '; }

expect_status() { # label expected url [curl args...]
    local label=$1 want=$2 got
    shift 2
    got=$(status "$@")
    if [[ "$got" == "$want" ]]; then pass "$label ($got)"; else fail "$label: expected $want, got $got"; fi
}

expect_header() { # label headers-text name [substring]
    local label=$1 text=$2 name=$3 want=${4:-} line count
    count=$(grep -c "^${name}: " <<<"$text")
    line=$(grep -m1 "^${name}: " <<<"$text")
    if [[ "$count" -eq 0 ]]; then
        fail "$label: no ${name}"
    elif [[ "$count" -gt 1 ]]; then
        fail "$label: ${name} sent ${count} times"
    elif [[ -n "$want" && "$line" != *"$want"* ]]; then
        fail "$label: ${name} lacks '${want}' (${line#*: })"
    else
        pass "$label: ${name}${want:+ ($want)}"
    fi
}

echo "Smoke test of ${BASE}"

echo "HTTPS"
expect_status "GET / over HTTPS" 200 "${BASE}/"
if [[ "$HOST" != localhost* ]]; then
    redirect=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code} %{redirect_url}' "http://${HOST}/" 2>/dev/null || echo 000)
    if [[ "$redirect" == 30[178]\ https://* ]]; then pass "http:// redirects to https:// (${redirect%% *})"; else fail "http:// redirect: got '${redirect}'"; fi
fi

echo "Security headers on / (UI)"
ui=$(headers "${BASE}/")
expect_header "/" "$ui" strict-transport-security "max-age="
expect_header "/" "$ui" content-security-policy "connect-src 'self'"
expect_header "/" "$ui" content-security-policy "frame-ancestors 'none'"
expect_header "/" "$ui" x-content-type-options nosniff
expect_header "/" "$ui" x-frame-options DENY
expect_header "/" "$ui" referrer-policy
expect_header "/" "$ui" permissions-policy
expect_header "/" "$ui" cross-origin-opener-policy same-origin
if grep -Eq "^content-security-policy: .*connect-src 'self' [^;]" <<<"$ui"; then
    fail "/: connect-src allows more than 'self' (same-origin deploy expected)"
fi
if grep -q "^server: " <<<"$ui"; then fail "/: Server header present ($(grep -m1 '^server: ' <<<"$ui"))"; else pass "/: no Server header"; fi

echo "Security headers on /api/health (API)"
api=$(headers "${BASE}/api/health")
expect_status "GET /api/health" 200 "${BASE}/api/health"
expect_header "/api/health" "$api" strict-transport-security "max-age="
expect_header "/api/health" "$api" content-security-policy "default-src 'none'"
expect_header "/api/health" "$api" x-content-type-options nosniff
expect_header "/api/health" "$api" x-frame-options DENY
expect_header "/api/health" "$api" referrer-policy no-referrer
expect_header "/api/health" "$api" cache-control no-store
if grep -q "^server: " <<<"$api"; then fail "/api/health: Server header present"; else pass "/api/health: no Server header"; fi

echo "Public surface"
expect_status "GET /api/mode without a session" 401 "${BASE}/api/mode"
expect_status "GET /api/docs (not served with auth on)" 404 "${BASE}/api/docs"
expect_status "GET /api/openapi.json" 404 "${BASE}/api/openapi.json"
expect_status "POST /api/webhooks/stripe without a signature" 400 "${BASE}/api/webhooks/stripe" \
    -X POST -H 'Content-Type: application/json' --data '{}'

if [[ "$fails" -gt 0 ]]; then
    echo "${fails} check(s) failed"
    exit 1
fi
echo "All checks passed"
