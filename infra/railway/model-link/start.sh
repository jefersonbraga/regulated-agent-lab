#!/bin/sh
# Railway "model-link": joins the tailnet (userspace networking: no TUN device, no extra
# privileges) and forwards plain TCP on $PORT to the on-prem model, MODEL_TARGET (host:port on the
# tailnet), through tailscaled's local SOCKS5 proxy. It has no public domain: only the api service
# reaches it, at http://model-link.railway.internal:$PORT/v1. vLLM still checks its own API key.
#
#   TS_AUTHKEY    tagged, reusable, ephemeral auth key (the node is removed when it goes away)
#   MODEL_TARGET  e.g. 100.x.y.z:8000 (a tailnet IP; MagicDNS names work too)
#   TS_HOSTNAME   name in the admin console (default railway-model-link)
#   PORT          listen port (default 8000)
set -eu
: "${TS_AUTHKEY:?set TS_AUTHKEY}"
: "${MODEL_TARGET:?set MODEL_TARGET (host:port on the tailnet)}"
PORT="${PORT:-8000}"
SOCK=/tmp/tailscaled.sock

tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 \
    --state=mem: --statedir=/tmp/tailscale --socket="$SOCK" &
TSD=$!

i=0
until [ -S "$SOCK" ]; do
    i=$((i + 1)); [ "$i" -gt 30 ] && { echo "tailscaled did not start" >&2; exit 1; }
    sleep 1
done
tailscale --socket="$SOCK" up --authkey="$TS_AUTHKEY" \
    --hostname="${TS_HOSTNAME:-railway-model-link}" --accept-dns=false --timeout=60s
echo "model-link: tailnet up, forwarding :$PORT -> $MODEL_TARGET"

# --experimental: socat 1.8.0 still flags its SOCKS5 address type as experimental.
socat --experimental TCP-LISTEN:"$PORT",fork,reuseaddr \
    SOCKS5-CONNECT:127.0.0.1:1055:"${MODEL_TARGET%:*}":"${MODEL_TARGET##*:}" &
SOCAT=$!

# Keep the path to the model warm: after a few idle minutes tailscaled drops the peer from its
# WireGuard config, and the first request after that must re-establish the path through the
# home router, which can stall past the API's LLM timeout. A TCP connect every KEEP_WARM_S
# seconds (open, then close: vLLM sees no request) keeps the peer active. Failures are ignored.
KEEP_WARM_S="${KEEP_WARM_S:-30}"
(
    while sleep "$KEEP_WARM_S"; do
        timeout 10 socat --experimental -u /dev/null \
            SOCKS5-CONNECT:127.0.0.1:1055:"${MODEL_TARGET%:*}":"${MODEL_TARGET##*:}" \
            >/dev/null 2>&1 || echo "model-link: keep-warm connect to $MODEL_TARGET failed" >&2
    done
) &
WARM=$!

# Either process exiting ends the container, so Railway restarts it cleanly.
trap 'kill "$SOCAT" "$TSD" "$WARM" 2>/dev/null; exit 0' TERM INT
while kill -0 "$TSD" 2>/dev/null && kill -0 "$SOCAT" 2>/dev/null; do sleep 5; done
echo "model-link: tailscaled or socat exited" >&2
exit 1
