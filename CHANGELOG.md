# Changelog

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
