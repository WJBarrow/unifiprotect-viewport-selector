# Changelog

## 0.3.0 — hardening + multi-instance docs

- Constant-time bearer-token comparison (`hmac.compare_digest`) in the auth check.
- Pin `requests` in the Dockerfile for reproducible builds.
- README: document running multiple instances (e.g. home + camp) from shared
  source, the per-console local-account gotcha, and the stock Home app TV-UI
  caveat (why the radio switches are the better default).
- Add `SECURITY_REPORT.md` (LAN-only assessment; no CRITICAL/HIGH findings, repo
  verified secret-free). Report is git-ignored.

## 0.2.0 — single-tile TV selector

- Add `homebridge-viewport-tv/`, a small Homebridge platform plugin that exposes
  the Viewport as one HomeKit **Television** accessory whose inputs are the Live
  Views — a single-tile alternative to the per-view radio switches. Reads views +
  current from `GET /health`, selects via `POST /select/on`; no new service
  endpoints. Publishes as an external accessory; install via a `file:` dependency
  for Homebridge's strict plugin resolution.

## 0.1.0 — initial release

HomeKit Live-View selector for a UniFi Protect Viewport.

- One HTTP "switch" endpoint per Live View so a Homebridge HTTP-SWITCH accessory
  (stateful / radio-button mode) can pick the Viewport's Live View from the Home
  app — no webhook, no auto-revert.
- `GET|POST /select/on?view=` sets the Viewport(s); `GET /select/state?view=`
  returns plain `1`/`0` for `statusPattern`; `/select/off` is a documented no-op.
- Tile state read **live** from Protect's bootstrap (`viewer.liveview`), with a
  short TTL cache (`STATE_CACHE_TTL`) so the polling tiles don't hammer the NVR.
- `VIEWS` env var selects/orders which Live Views are exposed; optional
  `SELECT_TOKEN` bearer guards the `/select/*` endpoints.
- Reuses the session-cookie + CSRF Protect-API auth and viewport/liveview
  resolution from the sibling `unifiprotect-viewport` project. Default port 8787.
