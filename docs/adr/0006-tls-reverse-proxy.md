# ADR 0006: TLS reverse proxy in front of OpenWebUI

**Status:** Accepted
**Date:** 2026-08-24
**Decision owners:** EedgeAI maintainers

## Context

OpenWebUI's `:8080` is the one port this stack intentionally publishes
beyond `127.0.0.1` — the human-facing front door (ADR 0002). It carried
plaintext HTTP: session cookies, the `WEBUI_SECRET_KEY`-signed JWT, and
every chat message crossed the network unencrypted. The README already
flagged this ("Put a real TLS-terminating reverse proxy in front of this
for anything beyond a trusted LAN — this compose file does not provide
one"), but nothing did it.

## Decision

Add a `caddy` service (`caddy:2.8-alpine`) in front of `openwebui`:

- `openwebui`'s port binding moves to `127.0.0.1:8080:8080` — same
  operator/admin-tunnel pattern as every other internal service in this
  stack, no longer the public port.
- `caddy` publishes `8443:8443` and reverse-proxies to `openwebui:8080`
  (`caddy/Caddyfile`). It is now the port end users hit.
- TLS is served with `tls internal { on_demand }`: Caddy mints and persists
  its own local CA + leaf certificates (`caddy_data` volume) instead of
  requesting one from a public CA. This needs no domain name and no
  internet-reachable port 80/443, which fits how this stack is actually
  deployed today (LAN / dev box / tunnel), and it means the wire is
  encrypted immediately with zero configuration.

  `on_demand` is required, not optional: the site block in
  `caddy/Caddyfile` has no static hostname, since this port is reached by
  whatever address the client used — `localhost` through an SSH tunnel, a
  LAN IP, or a real hostname. Verified directly (spin up `caddy` + a
  stand-in backend on an isolated network, connect with different SNI
  values): a bare `tls internal` block with no `on_demand` never issues a
  certificate for an address it wasn't told about upfront — every
  handshake fails with a generic "internal error" TLS alert, for both an
  IP-literal and a hostname client. `on_demand` mints a certificate the
  first time each distinct address is actually used and caches it after
  that. This is safe specifically because the CA is internal/local — the
  "on-demand issuance needs an explicit permission policy" guard Caddy
  normally enforces exists to stop a public ACME CA's rate limits being
  exhausted by unbounded requests, which doesn't apply here.

Caddy was chosen over a bare nginx/TLS config for this because
`tls internal` (automatic self-signed cert issuance + persistence + reload)
is built in — no separate `openssl req` step or cert-renewal cron to
maintain.

## Consequences

- Every browser gets a one-time "certificate not trusted" warning until
  Caddy's local CA root is trusted on the client, or a real certificate is
  installed. This is expected and matches the "self-signed for now"
  decision — it is not a bug in the Caddyfile.
- **If this stack is ever exposed on a real, internet-reachable domain**,
  replace `tls internal` in `caddy/Caddyfile` with the domain itself:
  ```
  your-domain.example.com {
      reverse_proxy openwebui:8080
  }
  ```
  and publish `80:80`/`443:443` instead of `8443:8443` — Caddy will then
  request and auto-renew a real Let's Encrypt certificate with no other
  changes. This was deliberately not the default here because it requires
  DNS and open inbound ports this deployment doesn't have.
- `WEBUI_SECRET_KEY` and `GCOR_API_KEY` were already required secrets
  (ADR 0002); TLS termination doesn't change that authentication story, it
  just stops those credentials and every chat payload from crossing the
  network in the clear.
- Direct `http://<host>:8080` access still works from the docker host
  itself (or via tunnel) for debugging — it was never removed, only
  unpublished from the network.
