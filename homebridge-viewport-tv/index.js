'use strict';

// Homebridge platform plugin: exposes a UniFi Protect Viewport's Live Views as
// a single HomeKit Television accessory, where each Live View is an "input".
// Picking an input drives the unifiprotect-viewport-selector service:
//   - reads available views + the current one from GET /health
//   - selects a view via POST /select/on?view=<name>
//
// Television accessories can't be bridged, so this publishes an *external*
// accessory (it still appears under the same bridge pairing automatically).

const http = require('http');
const { URL } = require('url');

let Service, Characteristic;

module.exports = (api) => {
  Service = api.hap.Service;
  Characteristic = api.hap.Characteristic;
  api.registerPlatform('homebridge-viewport-tv', 'ViewportTV', ViewportTVPlatform);
};

class ViewportTVPlatform {
  constructor(log, config, api) {
    this.log = log;
    this.api = api;
    this.name = (config && config.name) || 'Viewport';
    this.baseUrl = ((config && config.baseUrl) || 'http://127.0.0.1:8787').replace(/\/+$/, '');
    this.token = (config && config.token) || '';
    this.pollMs = (((config && config.pollInterval) || 4) * 1000);
    this.views = [];        // ordered Live View names; identifier = index + 1
    this.current = null;    // currently shown view name (or null)
    this.tv = null;

    if (!api) return;
    api.on('didFinishLaunching', () => this.init());
  }

  // --- HTTP to the selector service ------------------------------------
  request(method, path) {
    return new Promise((resolve, reject) => {
      let u;
      try { u = new URL(this.baseUrl + path); } catch (e) { return reject(e); }
      const req = http.request({
        method,
        hostname: u.hostname,
        port: u.port || 80,
        path: u.pathname + u.search,
        headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
      }, (res) => {
        let body = '';
        res.on('data', (c) => (body += c));
        res.on('end', () => {
          if (res.statusCode >= 400) {
            return reject(new Error(`HTTP ${res.statusCode}: ${body.slice(0, 120)}`));
          }
          resolve(body);
        });
      });
      req.on('error', reject);
      req.setTimeout(8000, () => req.destroy(new Error('timeout')));
      req.end();
    });
  }

  async getHealth() {
    return JSON.parse(await this.request('GET', '/health'));
  }

  identifierFor(name) {
    const i = this.views.indexOf(name);
    return i < 0 ? 0 : i + 1; // 0 = "no input selected" (e.g. a built-in layout)
  }

  // --- lifecycle --------------------------------------------------------
  async init() {
    try {
      const h = await this.getHealth();
      this.views = Array.isArray(h.views) ? h.views : [];
      this.current = h.current || null;
    } catch (e) {
      this.log.error(`cannot reach viewport service at ${this.baseUrl}: ${e.message}`);
    }
    if (!this.views.length) {
      this.log.error('no Live Views returned by the service — not publishing the TV accessory');
      return;
    }
    this.buildAccessory();
    this.poll();
    setInterval(() => this.poll(), this.pollMs);
  }

  buildAccessory() {
    const uuid = this.api.hap.uuid.generate('homebridge-viewport-tv:' + this.name);
    const acc = new this.api.platformAccessory(this.name, uuid, this.api.hap.Categories.TELEVISION);

    const tv = acc.addService(Service.Television);
    tv.setCharacteristic(Characteristic.ConfiguredName, this.name);
    tv.setCharacteristic(Characteristic.SleepDiscoveryMode,
      Characteristic.SleepDiscoveryMode.ALWAYS_DISCOVERABLE);

    // The viewport is always "on" — power is a no-op; only input matters.
    tv.getCharacteristic(Characteristic.Active)
      .onGet(() => Characteristic.Active.ACTIVE)
      .onSet(() => { /* ignore power toggles */ });

    tv.getCharacteristic(Characteristic.ActiveIdentifier)
      .onGet(() => this.identifierFor(this.current))
      .onSet(async (id) => {
        const name = this.views[Number(id) - 1];
        if (!name) return;
        try {
          await this.request('POST', '/select/on?view=' + encodeURIComponent(name));
          this.current = name;
          this.log.info(`selected view '${name}'`);
        } catch (e) {
          this.log.error(`select '${name}' failed: ${e.message}`);
        }
      });

    tv.getCharacteristic(Characteristic.RemoteKey).onSet(() => { /* unused */ });

    // One InputSource per Live View, linked to the Television service.
    this.views.forEach((name, idx) => {
      const id = idx + 1;
      const input = acc.addService(Service.InputSource, name, `view-${id}`);
      input
        .setCharacteristic(Characteristic.Identifier, id)
        .setCharacteristic(Characteristic.ConfiguredName, name)
        .setCharacteristic(Characteristic.IsConfigured, Characteristic.IsConfigured.CONFIGURED)
        .setCharacteristic(Characteristic.InputSourceType, Characteristic.InputSourceType.APPLICATION)
        .setCharacteristic(Characteristic.CurrentVisibilityState,
          Characteristic.CurrentVisibilityState.SHOWN);
      tv.addLinkedService(input);
    });

    this.tv = tv;
    this.api.publishExternalAccessories('homebridge-viewport-tv', [acc]);
    this.log.info(`published '${this.name}' TV selector with ${this.views.length} inputs`);
  }

  async poll() {
    try {
      const h = await this.getHealth();
      this.current = h.current || null;
      if (this.tv) {
        this.tv.updateCharacteristic(Characteristic.ActiveIdentifier, this.identifierFor(this.current));
      }
    } catch (e) {
      this.log.debug(`poll failed: ${e.message}`);
    }
  }
}
