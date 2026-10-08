# 038 — Reaching the application from outside the home network

Issue: none; the owner's request of 2026-10-08
Status: in progress

## Problem

The application can only be opened from the home network. The container publishes port
8083 on every interface of the Raspberry Pi, as plain HTTP, and nothing sits in front of it:
no TLS, no proxy, no route in from anywhere else. Tailscale is on the host, but only the
delivery pipeline uses it, to reach the host over SSH.

The owner wants to see the portfolio from anywhere, **in any browser** — not only on devices
where a VPN client can be installed.

The application cannot simply be put on the internet. Its authentication was designed for a
private network, and spec 003 says so where it accepts its trade-offs:

- the login throttle is keyed on the username, not the address, so anyone who can reach
  `/api/auth/login` can lock the owner out for fifteen minutes, again and again;
- there is no second factor;
- Argon2id runs on the event loop, about 270 ms a verification on the Pi;
- there are no security headers.

Fixing those would be a project of its own. This one keeps them private instead.

## Scope / Non-goals

- **Cloudflare Tunnel** carries the traffic. The connector, `cloudflared`, runs on the host
  as a systemd service and opens an outbound connection to Cloudflare, so nothing on the home
  router is opened.
- **Cloudflare Access** sits in front of the public hostname. Only the owner's email address
  is allowed, and nothing reaches the application until that check passes, so the
  application's own login page stays private.
- **The application's port is published on the host's loopback interface only.** That ends
  plain HTTP on the local network.
- **`Cache-Control: no-store`** on every response under `/api`.
- **`docs/operations.md` section 20** documents the setup, in order.

Non-goals:

- **Tailscale Serve and Funnel.** Serve needs the Tailscale client on every device, which is
  exactly what the owner does not want. Funnel would put the login page on the internet with
  nothing in front of it.
- **Forwarding a port on the router**, for the same reason as Funnel, plus a dynamic
  address to track.
- **Hardening the application's authentication**: a throttle by address, a second factor,
  Argon2id off the event loop, security headers. Access makes them unnecessary for this
  deployment. They are still worth doing before the application is ever reachable without
  Access.
- **Validating the Access token inside the application.** `cloudflared` does it for every
  request (R3), and doing it again would mean a network fetch of signing keys and a host
  name and an audience tag in the application's settings.
- **Accepting more than one origin.** With the port on loopback, the public hostname is the
  only way in, so it is the only origin.

## Verified vendor facts (Cloudflare)

Read from Cloudflare's own documentation on 2026-10-08. The `cloudflared` behaviour was also
read from its source and release notes on GitHub. The application calls nothing of
Cloudflare's, so this is not a provider in the sense of `docs/providers.md`, and is recorded
here and in `docs/operations.md` section 20 instead.

Tunnel
(`developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/`):

- A remotely-managed tunnel is created in the dashboard under Networking > Tunnels. A
  hostname is published on its **Routes** tab as a *Published application*: a subdomain, a
  domain, and a Service URL.
- **The domain must be on Cloudflare.** A subdomain of more than one level needs an Advanced
  Certificate, so the hostname is one level: `portfolio.<domain>`.
- "If you do not have an Access application in place, the published application will be
  available to anyone on the Internet." Cloudflare recommends creating the Access
  application **before** the route.
- A remotely-managed tunnel runs on its token alone, and "anyone with access to the token
  will be able to run the tunnel."
- `cloudflared tunnel run --token-file <PATH>` (`TUNNEL_TOKEN_FILE`) is for remotely-managed
  tunnels, from 2025.4.0.
  - The source reads the file and trims surrounding whitespace, so a trailing newline is
    harmless.
- **Since 2026.7.2, `cloudflared service install <TOKEN>` on Linux** writes the token to
  `/etc/cloudflared/token`, at mode 0600, and the unit it writes runs
  `tunnel run --token-file` on it (release note VULN-118896). Before that release the token
  sat in the unit's `ExecStart`, readable in `ps`.
- The generated unit is `/etc/systemd/system/cloudflared.service`, with `Type=notify`,
  `TimeoutStartSec=15`, `Restart=on-failure`, `RestartSec=5s`, `After=` and
  `Wants=network-online.target`, and `--no-autoupdate`.
- Cloudflare supports `cloudflared` versions within one year of the latest release.
- The Debian package comes from `pkg.cloudflare.com`, suite `any`, signed by
  `cloudflare-main.gpg`. The key was rolled over on 30 October 2025.
- If outbound traffic is filtered, the connector needs port 7844 out.

Access
(`developers.cloudflare.com/cloudflare-one/access-controls/`):

- "All Access applications are deny by default — a user must match an Allow policy before
  they are granted access."
- A self-hosted application is created under Zero Trust > Access controls > Applications,
  with a public hostname, policies, identity providers and a session duration.
- **One-time PIN** is not added by default. It is added under Integrations > Identity
  providers.
  - A PIN expires ten minutes after it is requested and is single use.
  - It is emailed only to an address an Access policy allows.
- The tunnel's origin parameter `access` (`required`, `teamName`, `audTag`) is called
  **Protect with Access** in the dashboard, under the route's *Additional application
  settings*. With it, `cloudflared` validates the Access token before it proxies a request.

Docker (`docs.docker.com/engine/network/port-publishing/`, and the Engine 28 release notes):

- "In releases older than 28.0.0, hosts within the same L2 segment (for example, hosts
  connected to the same network switch) can reach ports published to localhost." Engine
  28.0.0 fixed it (moby/moby#49325), so R1 needs Engine 28.0.0 or later on the host.

Assumed, not verified:

- the seat limit of the free Zero Trust plan;
- what a request without a valid Access token receives from `cloudflared` when Protect with
  Access is on. Acceptance criterion 6 checks it on the real deployment;
- the optional edge toggles, Always Use HTTPS and HSTS.

## Rulings

- **R1. The port is published on `127.0.0.1` only.**
  - `deploy/compose.yml` publishes `127.0.0.1:${PORTFOLIO_PORT}:8000`.
  - The connector runs on the same host and is the only thing that needs to reach it.
  - Docker applies its forwarding rules before a host firewall such as ufw sees the packet,
    so such a firewall does not close the port. The binding does.
  - The health check runs inside the container and `deploy.py` never uses the host port, so
    deployment and rollback are unchanged.
  - It holds on Docker Engine 28.0.0 or later only. `docs/deployment.md` lists that among the
    host's requirements, and section 20 asks for `docker version` when checking the setup.
- **R2. `cloudflared` is a host service, not a compose service.**
  - It must not share the application's `env_file`, which holds the application's
    credentials.
  - It must not be stopped, replaced or rolled back by a deployment of the application.
  - It is a prerequisite of the host, like Tailscale.
- **R3. Access with one Allow policy, the owner's email, and Protect with Access on the
  route.**
  - The Access application is created before the route, so the hostname is never public
    for a moment.
  - Protect with Access makes `cloudflared` refuse a request without a valid Access token.
    Deleting the Access application, or taking it off the hostname, then closes the route
    rather than opening it.
  - It cannot catch a policy that allows too much. Access signs a token for everyone its
    policy admits, so the Allow policy is the one place the owner's address alone is
    enforced.
  - Session duration: 24 hours.
- **R4. The tunnel token is a credential.**
  - It lives in `/etc/cloudflared/token`, owned by root, mode 0600, and the service reads it
    with `--token-file`.
  - It is pasted into that file from standard input, so it is never a command-line argument
    and never in shell history.
  - It is never in the repository or in GitHub.
  - The public hostname, the team name and the audience tag are not in the repository
    either: the documentation uses `portfolio.example`, `<team-name>` and `<aud-tag>`.
- **R5. `Cache-Control: no-store` on every response under `/api`.**
  - The API sent no `Cache-Control` at all, which left caching to whoever was in between,
    and the owner may open it on a machine somebody else uses next.
  - A pure ASGI middleware, `api/cache_control.py`, sits between `RequestContextMiddleware`
    and `RequestGuardMiddleware`, so the guard's 401s and 403s carry the header too.
  - A route that sets its own header keeps it. The single-page application's caching is
    `web/spa.py`'s and is not touched.
  - A 500 is sent by Starlette from outside every middleware of the application's own. Its
    body is a fixed problem document that carries nothing of the owner's.
- **R6. One origin: the public hostname.**
  - `PORTFOLIO_ALLOWED_ORIGIN` becomes `https://portfolio.<domain>`.
  - `PORTFOLIO_SESSION_COOKIE_SECURE` returns to its default, `true`, so the cookie is
    `__Host-psid` again.
- **R7. Nothing in the application's authentication changes.** The trade-offs spec 003
  accepted for "an application only reachable over a private network" stay accepted,
  because Access keeps the login page reachable by the owner alone.

## Acceptance criteria

In the repository:

1. `deploy/compose.yml` publishes the port on `127.0.0.1` and nowhere else.
   `tests/deploy/test_compose_ports.py`.
2. Every response under `/api` carries `Cache-Control: no-store`: 200, the guard's 401 and
   403, 404 and 422. The single-page application's index and assets keep their own values.
   `backend/tests/api/test_cache_control.py`.
3. `docs/operations.md` section 20 gives the setup in an order that never leaves the
   hostname public, with placeholders only. Its first section no longer offers plain HTTP on
   the local network.

On the deployment, checked by the owner once section 20 is done:

4. `curl -sI https://portfolio.<domain>/api/health`, with no cookie, never answers `200`.
   Access answers instead, normally with a redirect to `<team-name>.cloudflareaccess.com`.
5. From a phone on mobile data, the PIN arrives, the application's login works, the
   dashboard loads, and a write succeeds, such as renaming a wallet. No `403`.
6. With the Access application's policy temporarily changed to block, the hostname serves
   nothing of the application.
7. After this change is deployed, on Docker Engine 28.0.0 or later:
   - from another machine on the local network, `http://<host-address>:8083/` is refused;
   - on the host, `curl -s http://127.0.0.1:8083/api/health` answers `ok`.

## Risks

- **Cloudflare terminates TLS**, so the owner's data is in clear text on Cloudflare's
  network. Accepted by the owner when choosing any-browser access over a VPN client.
- **A Cloudflare outage means no browser access.** The timers keep reading balances and
  prices, and nothing is lost.
- **Merging before the tunnel works removes access from the local network** until it is set
  up. The pull request says so, and section 20 is ordered to avoid it. The command-line
  tools still work over SSH.
- **An Access session that expires mid-use breaks the API calls.** The browser's `fetch`
  cannot follow Access's cross-origin redirect to the PIN page. Reloading the page asks for
  a PIN again. Documented in the troubleshooting table.
- **The hostname is public.** It appears in Certificate Transparency logs and in DNS. Only
  the name, not the content.
- **Removing both the Access application and Protect with Access** would leave the
  application on the internet with only its own password. Both are deliberate acts in the
  dashboard, and section 20 says what each one is for.
- **An Allow policy widened beyond the owner's address** lets whoever it admits reach the
  application's sign-in, and nothing after Access notices. That brings back the lockout and
  brute-force exposure spec 003 accepted only for a private network.
- **A leaked tunnel token** lets someone else run a connector for the tunnel and receive
  some of its traffic. The token can be rotated from the dashboard, and section 20 says how
  to replace the file.
