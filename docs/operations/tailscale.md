# Operations Runbook — Bringing the Dashboard Online via Tailscale

> Scope: PRD §5.9 / §9 Phase 3 "Tailscale Serve 正式上線". This is an
> **operator runbook executed on the PolyEcho host** (your Mac, later the
> GCE e2-micro). Steps marked **[HOST]** must be run by you on that machine —
> they cannot be performed from the dev/agent environment. Config files live in
> [`infra/tailscale/`](../../infra/tailscale/) (see its README for the raw
> reference); this document is the ordered end-to-end procedure.

## 0. Prerequisites (one-time)

- **[HOST]** Tailscale installed and logged in on the host:
  `tailscale up` → sign in as `yslinear@gmail.com`.
- **[HOST]** Tailscale installed on the iPhone, same account.
- Apply the ACL: copy [`infra/tailscale/acl.example.hujson`](../../infra/tailscale/acl.example.hujson)
  into the [Tailscale Admin Console](https://login.tailscale.com/admin/acls).
  It restricts ports 443/22 on `tag:polyecho-server` to your own devices only.
- **[HOST]** Tag the host: in the admin console assign `tag:polyecho-server`
  to this machine (matches the ACL and the SSH rule).

## 1. Start the stack (host stays clean — all in Docker)

```bash
# [HOST] from the repo root
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps          # db/web/core/discovery/push healthy
```

The dashboard listens on `127.0.0.1:3000` only (never 0.0.0.0 — PRD §4.2).
Confirm locally before exposing it:

```bash
# [HOST]
curl -fsS http://localhost:3000 >/dev/null && echo "dashboard up"
```

## 2. Expose it on the tailnet (HTTPS, tailnet-only)

```bash
# [HOST] declarative (preferred — matches infra/tailscale/serve.json)
tailscale serve --set-raw - < infra/tailscale/serve.json

# …or the imperative equivalent:
tailscale serve --bg --https=443 http://localhost:3000
```

Reachable at: `https://<machine-name>.<tailnet>.ts.net`

## 3. Verify

```bash
# [HOST] serve config present, Funnel ABSENT
tailscale serve status            # shows :443 → http://127.0.0.1:3000 ; NO "Funnel"

# [HOST] end-to-end over the tailnet
curl -fsS "https://$(tailscale status --json \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')" \
  >/dev/null && echo "tailnet HTTPS OK"

# health check (Config / DB / Polymarket API / Tailnet)
docker compose -f infra/docker-compose.yml run --rm cli polyecho doctor
```

`polyecho doctor`'s **Tailnet** row should report the tailnet as reachable
(it shells `tailscale status --json`; "not installed / not on tailnet" is
expected only inside containers, not on the host).

## 4. iPhone (required for Phase 5 Web Push)

1. iPhone on the tailnet (Tailscale app, same account).
2. Safari → `https://<machine-name>.<tailnet>.ts.net`.
3. **Share → Add to Home Screen** — iOS only delivers Web Push to an
   installed PWA (PRD §5.8); do this now so Phase 5 works later.

## ⚠️ NEVER enable Tailscale Funnel

**Funnel publishes the host to the public internet. The dashboard has *no*
auth layer by design — the entire security model is Tailscale network-layer
isolation (PRD §7.2 / §5.9). Funnel would expose it to the world. Do not
enable it under any circumstances.** `tailscale serve` (above) does not enable
Funnel; re-verify any time with `tailscale serve status` (must not show
"Funnel").

## Ongoing operations

- **Restart after reboot:** `serve` config persists; just ensure
  `docker compose … up -d` runs (host login item / launchd / `restart: unless-stopped`).
- **Rotate/parity check:** `polyecho doctor` + `tailscale serve status` after any host change.
- **Travel mode (PRD §3.2 情境 D):** set conservative settings in the
  dashboard *before* leaving; Tailscale + Web Push keep working remotely,
  remote control needs tailnet connectivity (expected limitation).
- **Take it offline:** `tailscale serve reset` then
  `docker compose -f infra/docker-compose.yml down`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `ts.net` URL not resolving | Both devices on tailnet? `tailscale status`; MagicDNS enabled in admin console |
| 502 / connection refused | `docker compose ps` (web healthy?); `curl localhost:3000` on host |
| `serve` shows nothing | Re-run step 2; check `tailscale serve status`; ensure not run inside a container |
| Cert error | Tailnet HTTPS cert is auto-provisioned; allow a minute on first serve |
| Funnel shown | **Immediately** `tailscale funnel reset` / `tailscale serve reset` and re-apply step 2 |
