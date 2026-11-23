# Deploy: one Oracle Cloud "Always Free" ARM VM, one domain, HTTPS

```
Internet ──443/80──> caddy (TLS, Let's Encrypt) ──/api/*──> api:8000  (FastAPI + MCP subprocesses, /app/var volume)
                                                └─ else ──> ui:8080   (nginx, static console)
```

- `docker-compose.prod.yml`: `caddy` (the only service with published ports), `api`, `ui`;
  healthchecks, `restart: unless-stopped`, `no-new-privileges`, all capabilities dropped (Caddy
  keeps `NET_BIND_SERVICE`), rotated logs. Every image is multi-arch (linux/arm64 included).
- `Caddyfile`: automatic HTTPS for `$DOMAIN`; `/api/*` goes to the API with the prefix
  stripped and `flush_interval -1` (server-sent events stream through unbuffered); everything
  else goes to the UI. The security headers are the app's own. Caddy adds HSTS only to responses
  that lack it (no duplicates) and removes `Server`.
- **Same origin**: the UI is built with `VITE_API_URL=/api`, so the CSP's `connect-src` is
  just `'self'` and the `SameSite=Strict` session cookie is always sent. The API allows the
  origin of `PUBLIC_APP_URL` (`https://DOMAIN`) for CORS and its Origin check, so
  `API_EXTRA_CORS_ORIGINS` can stay empty.
- **Client IPs**: Caddy replaces any incoming `X-Forwarded-For` with the real client address.
  uvicorn runs with `--proxy-headers --forwarded-allow-ips=$CADDY_IP` (Caddy's fixed address on
  the compose network), so the per-IP login rate limit sees real IPs and no other container or
  client can spoof them.
- `COOKIE_SECURE=1` and `API_AUTH_DISABLED=0` are enforced by the compose file.

All commands below run on the VM, from the repository root, unless they say otherwise.
`$C` is short for the compose invocation:

```bash
C="docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod"
```

## 1. Create the VM (Oracle Cloud console)

1. **Compute → Instances → Create instance.**
   - Image: **Canonical Ubuntu 24.04** (the aarch64 build is picked automatically for Ampere).
   - Shape: **Ampere → VM.Standard.A1.Flex**. The Always Free allowance is 4 OCPUs and 24 GB of
     RAM in total across all A1 instances. **2 OCPUs / 12 GB** is plenty for this stack (the
     API image builds in a few minutes). Use all 4 / 24 if you run nothing else.
   - Boot volume: the default 47-50 GB is enough (Always Free covers 200 GB of block storage).
   - Networking: create a new VCN with a **public subnet** and **assign a public IPv4 address**.
   - SSH: upload your public key (or download the generated private key).
2. **"Out of capacity for shape VM.Standard.A1.Flex"** is common. Try another **availability
   domain** (the "Placement" section of the same form), or retry later because capacity frees
   up during the day. Always Free compute is only free in your tenancy's **home region**,
   chosen at sign-up and fixed after that. If the home region stays full, a new account with a
   less busy home region is the only "other region" option. Upgrading the account to Pay As
   You Go usually makes A1 capacity much easier to get, and it stays free within the Always
   Free limits.
3. **Reserve the public IP** so it survives a stop/start: **Networking → IP management →
   Reserved public IPs → Reserve**. Then on the instance: **Attached VNICs → (VNIC) → IPv4
   addresses → (primary) ⋮ → Edit → No public IP → Update**, edit again → **Reserved public
   IP** → pick the reserved one → **Update**.
4. **Open TCP 80 and 443 in the VCN**: **Networking → Virtual cloud networks → (VCN) →
   Subnets → (public subnet) → Security lists → Default security list → Add ingress rules**:
   - Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port range `80`
   - Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port range `443`
   - Optional, for HTTP/3: source `0.0.0.0/0`, IP protocol UDP, destination port `443`

   Port 80 is needed: Let's Encrypt validates over it, and Caddy redirects it to HTTPS.

## 2. Prepare Ubuntu

```bash
ssh ubuntu@<reserved-ip>
sudo apt-get update && sudo apt-get -y upgrade
```

**Host firewall.** Oracle's Ubuntu images ship iptables rules (`/etc/iptables/rules.v4`,
loaded by `netfilter-persistent` at boot) that reject everything except SSH. Open 80/443
**before installing Docker**, so the saved rules do not capture Docker's own chains:

```bash
sudo iptables -I INPUT -p tcp -m state --state NEW -m multiport --dports 80,443 -j ACCEPT
sudo iptables -I INPUT -p udp -m state --state NEW --dport 443 -j ACCEPT
sudo netfilter-persistent save            # writes /etc/iptables/rules.v4 (survives reboots)
sudo iptables -L INPUT -n --line-numbers  # the two ACCEPT rules come before the final REJECT
```

Docker adds its own forwarding rules for published ports at every start. The INPUT rules
cover the host side. If Docker is already installed, do not run `netfilter-persistent save`.
Instead, add the same two lines to `/etc/iptables/rules.v4` just above the first
`-A INPUT -j REJECT` line, then reboot.

**Docker Engine + compose plugin** (official repository):

```bash
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER" && exit   # log in again for the group to apply
```

```bash
ssh ubuntu@<reserved-ip>
docker run --rm hello-world && docker compose version
```

## 3. A domain name (free options)

- **DuckDNS**: sign in at duckdns.org, create a subdomain (e.g. `lab-demo`), set its IP to
  the reserved IP. `DOMAIN=lab-demo.duckdns.org`.
- **sslip.io**: nothing to register. `203-0-113-10.sslip.io` resolves to `203.0.113.10`, so
  `DOMAIN=<ip-with-dashes>.sslip.io`. It is a shared domain, so certificate issuance can
  occasionally hit Let's Encrypt rate limits. DuckDNS is the steadier choice.
- Your own domain: an `A` record to the reserved IP.

Check before starting: `dig +short $DOMAIN` must print the reserved IP.

## 4. Configure

```bash
git clone <repository-url> regulated-agent-lab && cd regulated-agent-lab
cp infra/.env.prod.example infra/.env.prod && chmod 600 infra/.env.prod
C="docker compose -f infra/docker-compose.prod.yml --env-file infra/.env.prod"
```

Edit `infra/.env.prod` (`nano infra/.env.prod`). Every variable is commented there. At least:

| Variable | Value |
|---|---|
| `DOMAIN` | `lab-demo.duckdns.org` (no scheme) |
| `PUBLIC_APP_URL` | `https://lab-demo.duckdns.org` |
| `SESSION_SECRET`, `AGENT_TOKEN_SECRET` | two different outputs of `openssl rand -hex 32` |
| `DEMO_USERS` | see below, **single-quoted** |
| `REQUIRE_SEPARATE_APPROVER` | `1` (a requester and an approver account) |
| `LLM_PROVIDER` | `mock`, `anthropic` or `openai` (never `vllm`: the on-prem model is not reachable from here) |
| `LLM_MODEL` + `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | for a cloud provider |
| `TRAVEL_SEARCH_SOURCE` | `live` |
| `PAYMENTS_PROVIDER`, `PAYMENTS_FLOW` | `stripe_test`, `both` |
| `STRIPE_TEST_SECRET_KEY` | your test-mode `rk_test_...` (or `sk_test_...`) key |
| `STRIPE_WEBHOOK_SECRET` | after step 6 |

**Password hashes**, made inside the API image (no Python needed on the VM; the password is
asked twice without echo and never printed):

```bash
$C build api
$C run --rm --no-deps api ral-hash-password alice --role requester
$C run --rm --no-deps api ral-hash-password bob --role approver
```

Join the printed lines with a comma into one single-quoted value:
`DEMO_USERS='alice:requester:scrypt$...$...,bob:approver:scrypt$...$...'`.

## 5. Start and verify

```bash
$C up -d --build
$C ps                      # api, ui: healthy; caddy: up/healthy
$C logs -f caddy           # "certificate obtained successfully" for your domain (Ctrl-C)
```

```bash
curl -I https://$DOMAIN/                  # 200, CSP with connect-src 'self', HSTS, nosniff, DENY
curl -sI https://$DOMAIN/api/health       # API headers: default-src 'none', no-store, HSTS
curl -s https://$DOMAIN/api/health        # {"status":"ok"}
scripts/smoke_prod.sh $DOMAIN             # HTTPS, headers on / and /api/health, 401/404/400 checks
```

The smoke script checks that HTTP redirects to HTTPS, the security headers are present exactly
once on `/` and `/api/health`, `/api/mode` answers 401 without a session, `/api/docs` and
`/api/openapi.json` answer 404, and an unsigned webhook answers 400. A 503 there means
`STRIPE_WEBHOOK_SECRET` is not set yet.
Then sign in at `https://$DOMAIN/` with the accounts from step 4.

## 6. Stripe (test mode)

1. Dashboard in **test mode** → **Developers → Webhooks → Add endpoint** (newer dashboards:
   **Workbench → Webhooks → Create an event destination**, "Your account", Webhook endpoint):
   - URL: `https://DOMAIN/api/webhooks/stripe`
   - Events: `checkout.session.completed`, `checkout.session.expired`,
     `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`,
     `charge.dispute.created`
2. Reveal the endpoint's **signing secret** (`whsec_...`), put it in `infra/.env.prod` as
   `STRIPE_WEBHOOK_SECRET`, and apply it: `$C up -d --force-recreate api`.
3. **Public business name**: **Settings → Business → Public details** → set the public business
   name (and statement descriptor) to the fictional operator, `Demo Rail`. Checkout pages and
   receipts show it instead of your account's legal name.
4. Test: run a booking with hosted Checkout and card `4242 4242 4242 4242`. The run settles
   from the webhook, and the dashboard's endpoint page lists the deliveries with status 200.

## Operations

**Backups.** Everything stateful is in the `ral-prod_lab_var` volume (audit log + head, `lab.db`,
`payments.db`, `wallet.db`). Stop the API for a consistent SQLite copy (a few seconds):

```bash
mkdir -p ~/backups
$C stop api
docker run --rm -v ral-prod_lab_var:/data:ro -v ~/backups:/backup alpine \
  tar czf /backup/lab-var-$(date +%F-%H%M).tgz -C /data .
$C start api
```

Copy `~/backups` off the VM (`scp`), and keep a copy of `infra/.env.prod` somewhere safe and
private: paused runs need the same `AGENT_TOKEN_SECRET`. To restore:
`$C stop api && docker run --rm -v ral-prod_lab_var:/data -v ~/backups:/backup alpine sh -c
'rm -rf /data/* && tar xzf /backup/<file>.tgz -C /data' && $C start api`.
`ral-prod_caddy_data` holds the certificates. Losing it only means Caddy requests new ones.

**Update.**

```bash
git pull
$C up -d --build
docker image prune -f        # drop the previous image layers
```

**Reset the demo data** (runs, audit log, payments ledger, wallet; certificates are kept):

```bash
$C stop api && $C rm -f api
docker volume rm ral-prod_lab_var
$C up -d
```

Do not use `$C down -v`: it also deletes `caddy_data`, so every reset would request new
certificates and could hit Let's Encrypt's rate limits.

**Logs:** `$C logs --tail 200 api`. The logs are rotated at 5 x 10 MB per container.

**Idle-instance reclamation.** Oracle may reclaim (stop) Always Free instances that look idle
over 7 days: CPU at the 95th percentile below 20 %, network below 20 %, and for A1 also memory
below 20 %. A lightly used demo can qualify. Upgrading the tenancy to **Pay As You Go** stops
idle reclamation of Always Free resources, and you still pay nothing within the free limits
(set a budget alert anyway). Keep backups either way. A reclaimed instance can be started
again, and the reserved IP and boot volume stay.

## Railway (the live demo)

The public demo runs on Railway with the same `api` and `ui` images, plus two small services
from [`railway/`](railway/). The model stays on-prem: the `api` reaches the vLLM host over a
tailnet, through a service that holds the tailnet key so the API never does.

```
Internet ──HTTPS──> Railway edge ──> gateway (Caddy, the only public service)
                                        ├─ /api/*  ──> api:8000  (volume /app/var)
                                        └─ else    ──> ui:8080
api ──private network──> model-link:8000 ──tailnet (tag:railway)──> vLLM host :8000
```

| Service | Source | Public | Notes |
|---|---|---|---|
| `gateway` | `infra/railway/gateway` | yes (`*.up.railway.app`) | Caddy on `$PORT`, plain HTTP (Railway terminates TLS); same routes as `Caddyfile` above, so UI and API share one origin |
| `api` | `backend/` | no | volume at `/app/var`; `RAILWAY_RUN_UID=0` so the process can write the volume |
| `ui` | `frontend/` | no | build variable `VITE_API_URL=/api` |
| `model-link` | `infra/railway/model-link` | no | tailscaled in userspace mode + socat: `:8000` → `MODEL_TARGET` |

**Deploy.** Each service is uploaded from its own directory (`--path-as-root`). Upload a copy
holding only the files git tracks, so `.venv`, `node_modules`, `.env` and `backend/var/` never
leave the machine:

```bash
railway up <copy-of-backend>  --path-as-root -s api
railway up <copy-of-frontend> --path-as-root -s ui
railway up infra/railway/gateway    --path-as-root -s gateway
railway up infra/railway/model-link --path-as-root -s model-link
```

**API variables** (secrets set with `railway variable set NAME --stdin`, never on the command
line): `SESSION_SECRET`, `AGENT_TOKEN_SECRET`, `DEMO_USERS`, `STRIPE_TEST_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`, `VLLM_API_KEY`; plus `PUBLIC_APP_URL=https://<gateway domain>`,
`COOKIE_SECURE=1`, `API_AUTH_DISABLED=0`, `REQUIRE_SEPARATE_APPROVER=1`, `LLM_PROVIDER=vllm`,
`VLLM_BASE_URL=http://model-link.railway.internal:8000/v1`, `TRAVEL_SEARCH_SOURCE=live`,
`PAYMENTS_PROVIDER=stripe_test`, `FORWARDED_ALLOW_IPS=*` (the API has no public domain; only
the gateway reaches it). The Stripe webhook points at `https://<gateway domain>/api/webhooks/stripe`.

**Client IPs.** Railway's edge connects from `100.64.0.0/10` and overwrites `X-Real-IP` and
`X-Forwarded-For` with the client address (a value the client sends does not survive). The
gateway trusts only that range, takes the client IP from `X-Real-IP` and sends the API exactly
one address, so the per-IP login rate limit sees real, unspoofable IPs.

**Tailnet access.** The model-link node joins with a reusable, ephemeral auth key tagged
`tag:railway`. The policy lets that tag reach the vLLM port and nothing else, and no rule has it
as a destination:

```jsonc
"tagOwners": { "tag:railway": ["autogroup:admin"] },
{ "action": "accept", "src": ["tag:railway"], "dst": ["<vllm-host>:8000"] }
```

An untagged key joins as the key owner's device and gets that user's access, so check the tag
in the admin console after the first deploy.

**Incident: model answers over ~1 KB never arrived (path MTU black hole).** Runs failed with
`LLMUnavailable` after 2 x 60 s, but only for some requests: one-way trips worked, return trips
always failed. The same call from a laptop on the tailnet answered in 2.6 s, with identical
prompt, schema and library versions, so the model and the code were ruled out. Requests of
21 KB went through the tunnel; responses did not: up to ~950 bytes arrived, from ~1.4 KB on
nothing did. A return trip's intent is ~1.3 KB, the first answer to need a full-size packet.
Packets of ~1.36 KB on the wire (WireGuard over UDP) from the home network to Railway were
dropped without an ICMP "too big", so TCP never shrank them. Fix: `TS_DEBUG_MTU=1200` in the
model-link image. Its netstack then advertises a smaller MSS, and the model host sends segments
that fit. Nothing changed on the model host. Two follow-ups came out of it: the API now sends an
SSE keep-alive every 15 s while a step is quiet (a silent stream can be cut by proxies), and the
console shows a "still working" notice after 8 s without events.

**Keep the path warm.** After a few idle minutes tailscaled drops the peer from its WireGuard
config, and the first request must re-establish the path. The model-link opens a TCP connection
to the model every 30 s (`KEEP_WARM_S`) so that first request does not pay for it.

**Reset the demo data** (runs, audit log, payments ledger, wallet): delete the files under
`/app/var` with `railway ssh -s api`, then `railway redeploy -s api`. The audit chain restarts
and verifies.

**Cost.** Railway has no always-free tier (Hobby is a monthly subscription with included usage,
then usage-based). The Oracle VM above costs nothing within the Always Free limits.
