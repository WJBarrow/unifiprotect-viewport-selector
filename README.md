# UniFi Protect Viewport — HomeKit view selector

Pick which **Live View** a UniFi Protect **Viewport** displays, straight from
the Home app. Each Live View becomes a HomeKit switch tile; tapping one sets the
Viewport to that view and the other tiles flip off (radio-button behavior).

This replaces the multi-step Protect-app dance (site → devices → viewport →
settings → view) with a single tap. It is the companion to the sibling
`unifiprotect-viewport` project, but **there is no webhook and no auto-revert** —
selecting a view sets it and holds. Tile state is read *live* from Protect, so
HomeKit stays correct even when the view is changed from the Protect app.

## How it works

```
 Home app                Homebridge                 this service            Protect NVR
 ┌────────┐   tap on   ┌──────────────┐  POST     ┌──────────────┐  PATCH  ┌──────────┐
 │ Front  │──────────▶ │ HTTP-SWITCH  │─────────▶ │ /select/on   │───────▶ │ viewer   │
 │ Back ◀ │            │  (per view)  │           │  ?view=...   │         │ liveview │
 │ Drive  │◀───────────│  polls state │◀───────── │ /select/state│◀─────── │ bootstrap│
 └────────┘   1 / 0    └──────────────┘   "1"/"0" └──────────────┘  GET    └──────────┘
```

Each switch tile polls `GET /select/state?view=<name>`, which returns `1` only
for the Live View the Viewport is currently showing. Because state comes from
Protect's `bootstrap` (cached briefly), the tiles act as one radio group with no
extra coordination.

## Endpoints

| Method     | Path                      | Purpose                                            |
|------------|---------------------------|----------------------------------------------------|
| GET / POST | `/select/on?view=<name>`  | Set the Viewport(s) to `<name>`. → `{"view": ...}` |
| GET / POST | `/select/off?view=<name>` | No-op (radio groups can't be empty). → current     |
| GET        | `/select/state?view=<name>` | Plain `1`/`0` — is `<name>` the current view?     |
| GET        | `/` or `/status`          | HTML status: current view + exposed views          |
| GET        | `/health`                 | JSON `{status, current, views}`                    |

`GET` is accepted on `/select/on` and `/select/off` purely for manual `curl`
testing. If `SELECT_TOKEN` is set, every `/select/*` call must carry
`Authorization: Bearer <token>`.

## Configuration

Copy `.env.example` to `.env` and fill it in. Required: `UNIFI_HOST`,
`UNIFI_USER`, `UNIFI_PASSWORD`, and one of `VIEWPORT_NAME` / `VIEWER_ID`. Use a
**local** Protect admin account — Ubiquiti cloud SSO logins won't authenticate
against the console API.

Notable optional settings:

- `VIEWS` — comma-separated subset/order of Live View names to expose as
  switches. Empty exposes every Live View Protect reports.
- `SELECT_TOKEN` — bearer token guarding the `/select/*` endpoints.
- `HTTP_PORT` — defaults to `8787`.
- `STATE_CACHE_TTL` — seconds the bootstrap read backing `/select/state` is
  cached so the polling tiles don't hammer the NVR (default `2`).

## Run

```bash
cp .env.example .env   # then edit .env
docker compose up --build -d
docker compose logs -f         # confirm it lists your viewport + exposed views
```

Rebuild (`--build`) after editing `service.py` — the image bakes the source in.

Quick manual check:

```bash
curl localhost:8787/health
curl "localhost:8787/select/state?view=Backyard"     # 1 or 0
curl -X POST "localhost:8787/select/on?view=Backyard" # switches the Viewport
```

## Homebridge — one HTTP-SWITCH per Live View

Add a [`homebridge-http-switch`](https://github.com/Supereg/homebridge-http-switch)
accessory per Live View, all in **stateful** mode so they reflect live state:

```jsonc
{
  "accessory": "HTTP-SWITCH",
  "name": "Viewport: Backyard",
  "switchType": "stateful",
  "onUrl":     "http://<service-host>:8787/select/on?view=Backyard",
  "offUrl":    "http://<service-host>:8787/select/off?view=Backyard",
  "statusUrl": "http://<service-host>:8787/select/state?view=Backyard",
  "statusPattern": "1",
  "pullInterval": 4000
}
```

Repeat for each view (URL-encode spaces in the view name, e.g. `Front%20Yard`).
Tapping one tile POSTs its `onUrl`; the others read `0` on their next poll and
turn off — giving radio-button selection in the stock Home app.

If `SELECT_TOKEN` is set, give each URL the auth header, e.g.:

```jsonc
"onUrl": {
  "url": "http://<service-host>:8787/select/on?view=Backyard",
  "headers": { "Authorization": "Bearer <token>" }
}
```

## Notes

- Driving multiple viewports (`VIEWPORT_NAME=A,B`) moves them together; the tile
  state reflects the first/primary viewport.
- If the Viewport is showing a built-in layout (not one of your Live Views), all
  tiles read `0` until you pick a view.
