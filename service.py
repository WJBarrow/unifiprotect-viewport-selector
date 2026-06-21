#!/usr/bin/env python3
"""HomeKit Live View selector for a UniFi Protect Viewport.

Exposes one HTTP "switch" endpoint per Protect Live View so a Homebridge
HTTP-SWITCH accessory (in stateful / radio-button mode) can pick which Live
View a Viewport displays, straight from the Home app — no more digging through
Protect's site → devices → viewport → settings → view menus.

There is no webhook and no auto-revert: selecting a view sets it and holds.
Each tile's state is read *live* from Protect's bootstrap (viewer.liveview), so
HomeKit reflects the real on-screen view even when it's changed elsewhere.

Device client talks to the UniFi OS console's Protect API
(/proxy/protect/api/...) using session-cookie + X-CSRF-Token auth — the same
login flow as the sibling unifiprotect-viewport project.
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import requests
import urllib3

MAX_BODY = 64 * 1024  # cap request bodies to avoid memory exhaustion

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("service")


class Config:
    def __init__(self) -> None:
        self.unifi_host = os.environ.get("UNIFI_HOST", "").rstrip("/")
        self.unifi_user = os.environ.get("UNIFI_USER", "")
        self.unifi_password = os.environ.get("UNIFI_PASSWORD", "")
        self.viewport_name = os.environ.get("VIEWPORT_NAME", "")
        self.viewer_id = os.environ.get("VIEWER_ID", "")  # optional override
        # Optional comma-separated subset/order of Live Views to expose as
        # switches; empty = expose every Live View found in the bootstrap.
        self.views = os.environ.get("VIEWS", "")
        self.verify_ssl = os.environ.get("VERIFY_SSL", "false").lower() == "true"
        self.http_port = int(os.environ.get("HTTP_PORT", "8787"))
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.log_file = os.environ.get("LOG_FILE", "")
        # Optional bearer token guarding the /select endpoints (must match the
        # Authorization header configured in the Homebridge accessory).
        self.select_token = os.environ.get("SELECT_TOKEN", "")
        # Seconds to cache the bootstrap read that backs /select/state, so N
        # polling switch tiles share one request instead of hammering Protect.
        self.state_cache_ttl = float(os.environ.get("STATE_CACHE_TTL", "2"))

    def validate(self) -> None:
        missing = [k for k, v in [
            ("UNIFI_HOST", self.unifi_host),
            ("UNIFI_USER", self.unifi_user),
            ("UNIFI_PASSWORD", self.unifi_password),
        ] if not v]
        if not self.viewport_name and not self.viewer_id:
            missing.append("VIEWPORT_NAME (or VIEWER_ID)")
        if missing:
            sys.exit("ERROR: missing env vars: " + ", ".join(missing))


class ProtectError(Exception):
    pass


class ViewportClient:
    """UniFi OS Protect-API client: read viewers/liveviews, set a viewer's view.

    Auth mirrors the sibling unifiprotect-viewport project (session cookie +
    CSRF, auto re-login on 401), pointed at /proxy/protect/api/.
    """

    def __init__(self, config: Config):
        self.config = config
        self.s = requests.Session()
        self.s.verify = config.verify_ssl
        self.csrf: str | None = None
        # VIEWPORT_NAME / VIEWER_ID may be comma-separated lists of viewports.
        self.viewport_names = [n.strip() for n in (config.viewport_name or "").split(",") if n.strip()]
        self.explicit_ids = [i.strip() for i in (config.viewer_id or "").split(",") if i.strip()]
        self.view_filter = [v.strip() for v in (config.views or "").split(",") if v.strip()]
        self.viewer_ids: list[str] = []        # resolved viewer ids to drive
        self.viewer_label: dict[str, str] = {}  # viewer id -> name (for logging)
        self.views: dict[str, str] = {}        # liveview name -> id
        self.view_names: dict[str, str] = {}    # liveview id -> name
        self.exposed: list[str] = []           # view names to expose as switches
        self._lock = threading.Lock()           # serialize PATCH writes
        # Short-TTL bootstrap cache backing /select/state.
        self._cache_lock = threading.Lock()
        self._cached_boot: dict | None = None
        self._cached_at = 0.0

    # --- auth (ported from the viewport project) --------------------------
    def login(self) -> None:
        try:
            r = self.s.post(
                f"{self.config.unifi_host}/api/auth/login",
                json={"username": self.config.unifi_user,
                      "password": self.config.unifi_password},
                timeout=15,
            )
        except requests.RequestException as e:
            raise ProtectError(f"cannot reach {self.config.unifi_host}: {e}") from e
        self.csrf = r.headers.get("X-CSRF-Token") or self._csrf_from_cookie()
        if r.status_code != 200:
            raise ProtectError(f"login failed: HTTP {r.status_code} {r.text[:200]}")

    def _csrf_from_cookie(self) -> str | None:
        tok = self.s.cookies.get("TOKEN")
        if not tok:
            return None
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("csrfToken")
        except Exception:
            return None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    def _request(self, method, path, **kw):
        url = f"{self.config.unifi_host}{path}"
        try:
            r = self.s.request(method, url, headers=self._headers(), timeout=15, **kw)
            if r.status_code in (401, 403):
                self.login()
                r = self.s.request(method, url, headers=self._headers(), timeout=15, **kw)
        except requests.RequestException as e:
            raise ProtectError(f"cannot reach {self.config.unifi_host}: {e}") from e
        if r.status_code >= 400:
            raise ProtectError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # --- domain -----------------------------------------------------------
    def _bootstrap(self) -> dict:
        return self._request("GET", "/proxy/protect/api/bootstrap")

    def _cached_bootstrap(self) -> dict:
        """Bootstrap read shared across rapid /select/state polls (TTL cache)."""
        with self._cache_lock:
            now = time.monotonic()
            if self._cached_boot is None or now - self._cached_at >= self.config.state_cache_ttl:
                self._cached_boot = self._bootstrap()
                self._cached_at = now
            return self._cached_boot

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_boot = None

    def connect(self) -> None:
        """Login + resolve viewer ids, the name<->id Live View maps, and the
        list of views to expose as HomeKit switches."""
        self.login()
        boot = self._bootstrap()
        viewers = boot.get("viewers", []) or []
        liveviews = boot.get("liveviews", []) or []
        by_name = {v.get("name"): v.get("id") for v in viewers}
        id_to_name = {v.get("id"): v.get("name") for v in viewers}

        # Resolve the configured viewports (explicit ids + names) to viewer ids.
        resolved: list[str] = []
        for vid in self.explicit_ids:
            if vid not in id_to_name:
                raise ProtectError(f"viewer id '{vid}' not found in bootstrap")
            resolved.append(vid)
        for name in self.viewport_names:
            vid = by_name.get(name)
            if not vid:
                raise ProtectError(
                    f"viewport '{name}' not found; available viewers: {sorted(by_name)}")
            resolved.append(vid)
        seen: set[str] = set()
        self.viewer_ids = [v for v in resolved if not (v in seen or seen.add(v))]
        if not self.viewer_ids:
            raise ProtectError("no viewports configured (set VIEWPORT_NAME or VIEWER_ID)")
        self.viewer_label = {vid: id_to_name.get(vid, vid) for vid in self.viewer_ids}

        self.views = {lv.get("name"): lv.get("id") for lv in liveviews if lv.get("name")}
        self.view_names = {lv.get("id"): lv.get("name") for lv in liveviews if lv.get("id")}

        # Decide which views to expose as switches.
        if self.view_filter:
            unknown = [v for v in self.view_filter if v not in self.views]
            if unknown:
                raise ProtectError(
                    f"VIEWS names not found: {unknown}; available: {sorted(self.views)}")
            self.exposed = list(self.view_filter)  # preserve configured order
        else:
            self.exposed = sorted(self.views)

        self._cached_boot = boot
        self._cached_at = time.monotonic()
        log.info("connected: viewports=%s exposing views=%s",
                 [self.viewer_label[v] for v in self.viewer_ids], self.exposed)

    def has_view(self, name: str) -> bool:
        return name in self.views

    def _primary_viewer(self) -> str:
        return self.viewer_ids[0]

    def current_view_name(self) -> str | None:
        """Live View currently shown by the primary viewport (or None)."""
        boot = self._cached_bootstrap()
        cur = {v.get("id"): v.get("liveview") for v in boot.get("viewers", []) or []}
        lv_id = cur.get(self._primary_viewer())
        return self.view_names.get(lv_id) if lv_id else None

    def _set_liveview(self, viewer_id: str, liveview_id: str) -> None:
        with self._lock:
            self._request(
                "PATCH",
                f"/proxy/protect/api/viewers/{viewer_id}",
                json={"liveview": liveview_id},
            )

    def select(self, view_name: str) -> None:
        """Set every configured viewport to the named Live View."""
        lv_id = self.views.get(view_name)
        if not lv_id:
            raise ProtectError(f"unknown Live View '{view_name}'")
        for vid in self.viewer_ids:
            log.info("selecting '%s' (%s) on %s", view_name, lv_id, self.viewer_label[vid])
            self._set_liveview(vid, lv_id)
        self._invalidate_cache()  # next /select/state poll reflects the change


class Selector:
    """Thin controller over ViewportClient — no timers, no persisted state."""

    def __init__(self, config: Config):
        self.config = config
        self.client = ViewportClient(config)

    def connect(self) -> None:
        self.client.connect()

    @property
    def views(self) -> list[str]:
        return self.client.exposed

    def select(self, view_name: str) -> None:
        self.client.select(view_name)

    def current(self) -> str | None:
        return self.client.current_view_name()

    def get_state_dict(self) -> dict:
        return {"current": self.current(), "views": self.views}


class Handler(BaseHTTPRequestHandler):
    selector: Selector = None
    config: Config = None

    def log_message(self, fmt, *args):
        log.debug("HTTP %s — " + fmt, self.address_string(), *args)

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code: int, body: str):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _view_from_query(self) -> str:
        return parse_qs(urlparse(self.path).query).get("view", [""])[0]

    def _authorized(self) -> bool:
        token = self.config.select_token
        if not token:
            return True  # auth disabled
        return self.headers.get("Authorization", "") == f"Bearer {token}"

    def _drain_body(self):
        length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY)
        if length:
            self.rfile.read(length)

    def _do_select_on(self):
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        view = self._view_from_query()
        if not view:
            self._json(400, {"error": "missing ?view="})
            return
        if not self.selector.client.has_view(view):
            self._json(404, {"error": f"unknown Live View '{view}'"})
            return
        try:
            self.selector.select(view)
        except Exception as exc:
            log.exception("select failed: %s", exc)
            self._json(502, {"error": str(exc)})
            return
        self._json(200, {"view": view})

    def _do_select_off(self):
        # Radio groups can't be "empty" — turning a tile off is a no-op; it
        # re-polls back to 1 if it was the active view. Report current state.
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        self._json(200, {"current": self.selector.current()})

    def _do_select_state(self):
        # Plain "1"/"0" body for homebridge-http-switch statusPattern.
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        view = self._view_from_query()
        try:
            current = self.selector.current()
        except Exception as exc:
            log.exception("state read failed: %s", exc)
            self._json(502, {"error": str(exc)})
            return
        self._text(200, "1" if view and view == current else "0")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/select/state":
            self._do_select_state()
        elif path == "/select/on":
            self._do_select_on()        # GET allowed for manual testing
        elif path == "/select/off":
            self._do_select_off()
        elif path in ("/", "/status"):
            self._status_page()
        elif path == "/health":
            try:
                sd = self.selector.get_state_dict()
            except Exception as exc:
                self._json(502, {"status": "error", "error": str(exc)})
                return
            self._json(200, {"status": "ok", **sd})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        self._drain_body()  # ignore body; selection is driven by ?view=
        if path == "/select/on":
            self._do_select_on()
        elif path == "/select/off":
            self._do_select_off()
        else:
            self._json(404, {"error": "not found"})

    def _status_page(self):
        try:
            current = self.selector.current()
        except Exception as exc:
            current = f"(error: {exc})"
        rows = "".join(
            f"<li>{'➤ ' if v == current else '&nbsp;&nbsp;&nbsp;'}"
            f"{html_lib.escape(v)}</li>"
            for v in self.selector.views)
        html = (f"<h1>Viewport view selector — port {self.config.http_port}</h1>"
                f"<p>Current view: <b>{html_lib.escape(str(current))}</b></p>"
                f"<ul>{rows}</ul>")
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    config = Config()
    config.validate()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    if config.log_file:
        os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            config.log_file, maxBytes=10 * 1024 * 1024, backupCount=3)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)

    selector = Selector(config)
    try:
        selector.connect()
    except Exception as exc:
        sys.exit(f"ERROR: cannot connect to Protect: {exc}")

    Handler.selector = selector
    Handler.config = config
    server = HTTPServer(("0.0.0.0", config.http_port), Handler)

    def _shutdown(sig, _frame):
        log.info("signal %d — shutting down", sig)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Listening on 0.0.0.0:%d  (status → /)", config.http_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
