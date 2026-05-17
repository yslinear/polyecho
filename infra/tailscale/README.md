# Tailscale Setup for PolyEcho

PolyEcho uses **Tailscale Serve** to expose the Next.js dashboard over HTTPS
on your private tailnet — accessible from your Mac and iPhone without opening
any firewall ports to the public internet.

## Quick Start

Run the following on the PolyEcho host (macOS or GCE e2-micro):

```bash
# Serve the local Next.js dashboard over HTTPS on your tailnet (port 443)
tailscale serve --bg --https=443 http://localhost:3000
```

After this, the dashboard is reachable at:

```
https://<your-machine-name>.<your-tailnet>.ts.net
```

## Using serve.json (alternative declarative approach)

```bash
tailscale serve --set-raw - < infra/tailscale/serve.json
```

## ACL Configuration

Copy `acl.example.hujson` as a starting point and apply it in the
[Tailscale Admin Console](https://login.tailscale.com/admin/acls).

The example ACL:
- Allows only your own Google account (`yslinear@gmail.com`) to reach port 443 and 22 on the server
- Tags the PolyEcho host as `tag:polyecho-server` for clean rule targeting
- Grants SSH access from your devices to the server

## iOS Access

1. Install Tailscale on iPhone
2. Sign in with the same account
3. Open Safari → `https://<machine-name>.<tailnet>.ts.net`
4. Tap Share → "Add to Home Screen" (required for Web Push to work on iOS)

## **WARNING: Never enable Tailscale Funnel**

**Funnel routes traffic from the public internet to your machine. This would expose the PolyEcho dashboard — which has no authentication layer — to anyone on the internet. This is a critical security violation per PRD §7.2 and §5.9. The entire security model relies on Tailscale network-layer isolation. Do not enable Funnel under any circumstances.**

The `tailscale serve` command above does NOT enable Funnel. Verify with:

```bash
tailscale serve status   # should NOT show "Funnel" anywhere
```

## Verification

```bash
# Check serve config
tailscale serve status

# Ping from iPhone (both devices must be on tailnet)
# Open: https://<machine-name>.<tailnet>.ts.net

# Check from Mac
curl -k https://$(tailscale status --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))")
```
