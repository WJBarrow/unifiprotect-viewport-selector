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

## Alternative: a single "TV-style" tile (`homebridge-viewport-tv/`)

If you'd rather have **one** tile with a pick-one list instead of N switches,
this repo bundles a small Homebridge plugin in [`homebridge-viewport-tv/`](homebridge-viewport-tv/)
that exposes the Viewport as a HomeKit **Television** accessory — each Live View
is an "input". It reads the view list + current view from `GET /health` and
selects via `POST /select/on`, so the service needs no extra endpoints.

Install it into a Homebridge that uses `--strict-plugin-resolution` (the
official `homebridge/homebridge` Docker image does) by registering it as a
`file:` dependency so the boot-time install picks it up:

```bash
# copy the plugin where Homebridge can install from, then declare it
cp -r homebridge-viewport-tv <homebridge-dir>/plugins-src/homebridge-viewport-tv
#   add to <homebridge-dir>/package.json dependencies:
#     "homebridge-viewport-tv": "file:plugins-src/homebridge-viewport-tv"
docker exec -w /homebridge homebridge npm install ./plugins-src/homebridge-viewport-tv
```

Then add the platform to `config.json` and restart Homebridge:

```jsonc
"platforms": [
  {
    "platform": "ViewportTV",
    "name": "Viewport",
    "baseUrl": "http://127.0.0.1:8787",
    "token": "<SELECT_TOKEN>",
    "pollInterval": 4
  }
]
```

It publishes an **external** accessory (TVs can't be bridged) that still appears
under the same bridge pairing. In the Home app the tile lives under the TV remote
UI: opening it shows the input list; the current view is checkmarked and stays in
sync via the `pollInterval` (seconds). Trade-off vs. the radio switches: one tile,
but selecting a view is tap-to-open then pick rather than a single tap.

> Note: the stock Home app renders a Television accessory with a mandatory On/Off
> power button and hides the full input list under *Settings → Inputs* — that UI is
> fixed by Apple. If you want "tap in, see every view, one tap to pick", the
> radio-button switches above are the better fit. The TV tile shines when you have
> many views and prefer a single tile.

## Running multiple instances (e.g. home + camp)

Each instance drives **one** UniFi OS console / Protect NVR. To control viewports
on a second site, run a second container pointed at that NVR on its own port —
reusing this same source so there's only one `service.py` to maintain:

```yaml
# ../my-selector-camp/docker-compose.yml
services:
  service:
    build: ../unifiprotect-viewport-selector   # shared source
    restart: unless-stopped
    ports: ["${HTTP_PORT:-8788}:${HTTP_PORT:-8788}"]
    env_file: [.env]                            # camp NVR creds, HTTP_PORT=8788
    volumes: ["./logs:/app/logs"]
```

Give the second instance its own `SELECT_TOKEN`, and name its Homebridge tiles
distinctly (e.g. `Camp Viewport: <view>`) so they don't collide with the first
set. One host-networked Homebridge can drive any number of instances over
`127.0.0.1:<port>`.

**Per-console account gotcha:** UniFi accounts are per-device. The gateway and the
Protect NVR are often *separate devices with separate local admin accounts* — and a
cloud (SSO) login won't authenticate against the API at all. Point `UNIFI_HOST` at
the device that actually runs **Protect** (the NVR/NAS/Cloud Key), and use a
**local** admin account that exists on *that* device.

## Notes

- Driving multiple viewports (`VIEWPORT_NAME=A,B`) moves them together; the tile
  state reflects the first/primary viewport.
- When HomeKit first discovers the TV accessory it may push an initial input,
  which can switch the Viewport once; just pick the view you want afterwards.
- If the Viewport is showing a built-in layout (not one of your Live Views), all
  tiles read `0` until you pick a view.

## License

[MIT](LICENSE) © Warren Barrow. Not affiliated with or endorsed by Ubiquiti Inc.;
"UniFi" and "UniFi Protect" are trademarks of Ubiquiti.
