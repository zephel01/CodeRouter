# Remote access guide — reaching CodeRouter safely from another machine

日本語版: [`remote-access.md`](./remote-access.md)

This guide is for when you want to use the dashboard or the chat endpoints from a **different machine** than the one running CodeRouter — on the same LAN or from outside. Conclusion first: **you have four options; when in doubt pick the SSH tunnel (single user) or Tailscale (multiple devices)**. Raw LAN exposure via `--host 0.0.0.0` is not recommended outside a fully trusted network.

---

## The trust boundary, first

Two facts to internalize:

1. **The chat endpoints (`/v1/messages` / `/v1/chat/completions`) and the dashboard have no authentication.** Anyone who can reach the port can run inference on your models
2. **`CODEROUTER_ALLOWED_HOSTS` is not authentication.** It is Host-header validation (a DNS-rebinding guard for browser-borne attacks); it does not control who can connect directly

"Who can reach the port" must therefore be designed at the network layer. That is what this guide is about.

| Method | Best for | Effort | CodeRouter-side config |
|---|---|---|---|
| ① SSH tunnel | one person, one device, ad-hoc | low | **none** (stays loopback) |
| ② Tailscale | personal / small team, many devices, on the go | low | `ALLOWED_HOSTS` = Tailscale name/IP |
| ③ Reverse proxy + auth | teams, permanent, browser-heavy | medium | `ALLOWED_HOSTS` = public hostname |
| ④ Raw LAN + firewall | fully trusted home LAN only | low | `--host 0.0.0.0` + `ALLOWED_HOSTS` |

---

## ① SSH tunnel — smallest attack surface (first choice for one user)

The server keeps its **default loopback bind**; nothing is exposed. The client digs the tunnel.

```bash
# on the client (e.g. your MacBook)
ssh -N -L 8088:localhost:8088 you@<server-ip>

# from now on, the client's http://localhost:8088 reaches the server's CodeRouter
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

- `CODEROUTER_ALLOWED_HOSTS` is **not needed** (the Host stays localhost)
- Zero new attack surface — SSH's key auth is the gatekeeper
- Downside: one tunnel per device/session

## ② Tailscale — multiple devices, works from anywhere (recommended)

Install [Tailscale](https://tailscale.com/) (a WireGuard-based mesh VPN) on both machines and **LAN exposure becomes unnecessary**. Each device gets a private `100.x.y.z` address and a MagicDNS name (e.g. `my-server.tailnet-name.ts.net`), reachable only from devices in your tailnet.

```bash
# server — bind to the Tailscale IP only, allow-list its names
CODEROUTER_ALLOWED_HOSTS=my-server.tailnet-name.ts.net,100.x.y.z \
  coderouter-t serve --host 100.x.y.z --port 8088

# client (anywhere in the world, as long as it's in the tailnet)
open http://my-server.tailnet-name.ts.net:8088/dashboard
```

- The point is to **bind to the Tailscale IP**, not `0.0.0.0` — nothing opens on the physical LAN
- Authentication and encryption are Tailscale's job (per-device approval, key rotation included)
- The free tier is plenty for personal use; Claude Code from a laptop on the road uses the same URL

## ③ Reverse proxy + auth — teams and permanent exposure

For multiple users or browser-centric use, put an authenticating proxy in front. With [Caddy](https://caddyserver.com/) it's a few lines:

```
# Caddyfile — basic auth + HTTPS (internal CA)
coderouter.example.internal {
    tls internal
    basic_auth {
        alice $2a$14$...   # generate with `caddy hash-password`
    }
    reverse_proxy 127.0.0.1:8088
}
```

```bash
# CodeRouter stays loopback; allow the Host the proxy presents
CODEROUTER_ALLOWED_HOSTS=coderouter.example.internal coderouter-t serve --port 8088
```

- CodeRouter itself is never exposed (only the proxy reaches 127.0.0.1:8088)
- For Claude Code through the proxy, plan how the proxy credential travels (a `https://user:pass@host` base URL, or header injection at the proxy) — it is separate from `ANTHROPIC_AUTH_TOKEN`

## ④ Raw LAN exposure — fully trusted home LANs only

On a home LAN with only people you trust, this is fine and simple.

```bash
# on the server (e.g. 192.168.1.10) — ALLOWED_HOSTS is the SERVER's address (the one in the URL)
CODEROUTER_ALLOWED_HOSTS=192.168.1.10 coderouter-t serve --host 0.0.0.0 --port 8088
```

Two chores:

1. **Restrict source IPs with the OS firewall** (optional but recommended): e.g. `sudo ufw allow from 192.168.1.20 to any port 8088`, listing only the devices you allow
2. **Verify port 8088 is not reachable from the internet** (router port-forwards, UPnP). On networks that assign global IPs internally (universities, legacy corporate ranges), "on the LAN" can silently mean "on the internet"

> ⚠️ On shared-office LANs, campus networks, or anything with a guest Wi-Fi, do not use ④ — use ①–③. Anyone on the LAN could run inference on your models.

---

## Common mistakes

- **Putting the client's IP in `ALLOWED_HOSTS`** — the value is what appears in the client's URL bar, i.e. the **server's** address. Copying the value from the 403 error message (minus the port) is foolproof ([troubleshooting §1-6](./troubleshooting.en.md#1-6-other-machines-get-host--is-not-allowed-403--v270))
- **Assuming `ALLOWED_HOSTS` makes you safe** — as above, it is not authentication
- **Using Tailscale but still binding `0.0.0.0`** — that defeats the purpose; bind to the Tailscale IP

## Related

- [Security guide](./security.en.md) — the full trust-boundary / threat-model picture
- [Troubleshooting §1-6](./troubleshooting.en.md) — fixing `Host '...' is not allowed.` (403)
