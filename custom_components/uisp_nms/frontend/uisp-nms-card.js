// Front panel of a UISP-managed switch or router, with link, rates and PoE control.
// Adapted from the edgeswitch-card of marosmars/edgeswitch-homeassistant (MIT); see THIRD_PARTY_NOTICES.md.
// Bundled with the uisp_nms integration, which serves it at /uisp_nms/uisp-nms-card.js and loads it on every
// dashboard, so no Lovelace resource is needed. Config:
//   type: custom:uisp-nms-card
//   device: <device id or name>   the UISP device to show (the visual editor offers a picker)
//   protect: [1, 2]               ports that can't be disabled or cable-tested from the card (uplink / path to HA)
//   sfp: [9, 10]                  SFP ports (drawn as cages); default from the model
//   cycle_seconds: 5              off time for "Restart PoE" (service uisp_nms.power_cycle_port)
//   accent: '#26a69a'             header color
//   vlan_colors: {50: '#ef6c00'}  per-VLAN colors (default: a built-in palette; VLAN 1 stays uncolored)
// Tap a port for details and controls. Every action that cuts power or link, or changes the PoE voltage,
// needs a second tap within 4 s. Port enable/disable only appears when its "enabled" entity has been enabled.
// Under the ports, "Who's connected?" lists the devices the switch has seen on the selected port (service
// uisp_nms.get_port_clients) and "Cable test" tests its cable (uisp_nms.cable_test; the port loses link meanwhile).
const DOMAIN = 'uisp_nms';
const T = {
  temp: 'Temp', links: 'Links', powered: 'PoE on', off: 'Off', on: 'On', restart: 'Restart', again: 'Sure?',
  protected: 'Protected port (uplink / path to HA): it cannot be disabled or cable-tested from here',
  noName: 'no name', noLink: 'no link', portOff: 'port disabled', port: 'Port', output: 'Output',
  noDevice: 'No UISP NMS device found. Pick a device in the card settings.',
  offline: 'Disconnected from UISP', unavailable: 'No port data: the device is not connected to UISP.',
  pick: 'Tap a port for details and controls.', lgLink: 'link 1G+', lgSlow: 'link < 1G', lgDis: 'port disabled', lgPoe: 'PoE on',
  voltageWarn: 'Check the powered device supports this voltage', up: 'up',
  who: "Who's connected?", cable: 'Cable test', seen: 'seen', none: 'Nothing seen on this port.', via: 'via',
  cableWarn: 'The port loses link for a few seconds during the test', cableProt: 'Protected port: no cable test',
  pair: 'Pair', ports: 'ports',
  vlans: 'VLANs', lgUntag: 'untagged (port outline)', lgTag: 'tagged (dot)', untagged: 'untagged', tagged: 'tagged',
};
const VLAN_PALETTE = ['#42a5f5', '#ab47bc', '#26a69a', '#ef6c00', '#ec407a', '#7cb342', '#8d6e63', '#5c6bc0'];
// UISP PoE output names; '24v' is passive 24 V, 'active' is 802.3af/at (48 V).
const POE_LABEL = { '24v': '24V passive', active: '48V 802.3af/at', '54v': '54V passive', off: 'off' };
const POE_SHORT = { '24v': '24V', active: '48V', '54v': '54V' };
const SPEED = { '10000-full': '10G', '2500-full': '2.5G', '1000-full': '1G', '100-full': '100M', '100-half': '100M½', '10-full': '10M', '10-half': '10M½' };
const FAST = new Set(['1000-full', '2500-full', '10000-full']);
// SFP cages by model, where the model name does not give the RJ45 count.
const MODEL_SFP = { 'ES-10X': [9, 10], 'ES-10XP': [9, 10], 'ES-8-150W': [9, 10], 'ER-4': [3], 'ER-6P': [5], 'ER-12': [9, 10], 'ER-12P': [9, 10] };

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Patch the shadow DOM in place instead of replacing it: a fresh <ha-card> renders a moment later, so rebuilding it on
// every hass update briefly shrinks the page and the browser snaps the scroll position back to the top.
function paintCard(el, html) {
  const root = el.shadowRoot;
  const m = html.match(/^\s*<style>([\s\S]*?)<\/style>\s*<ha-card([^>]*)>([\s\S]*)<\/ha-card>\s*$/);
  if (!m) { root.innerHTML = html; root.__card = null; root.__html = null; return; }
  if (!root.__card) {
    root.innerHTML = '<style></style><ha-card><div class="paint-root" style="display:contents"></div></ha-card>';
    root.__style = root.querySelector('style');
    root.__card = root.querySelector('ha-card');
    root.__body = root.querySelector('.paint-root');
  }
  if (root.__style.textContent !== m[1]) root.__style.textContent = m[1];
  const attr = (n) => (m[2].match(new RegExp(`${n}="([^"]*)"`)) || [])[1] || '';
  if (root.__card.className !== attr('class')) root.__card.className = attr('class');
  if ((root.__card.getAttribute('style') || '') !== attr('style')) root.__card.setAttribute('style', attr('style'));
  if (root.__html !== m[3]) { root.__html = m[3]; root.__body.innerHTML = m[3]; }
}

// UISP NMS devices that have ports, as [device_id, device] sorted by name.
function uispDevices(hass) {
  const ids = new Set(Object.values(hass.entities || {})
    .filter((e) => e.platform === DOMAIN && e.translation_key === 'link' && e.device_id)
    .map((e) => e.device_id));
  return [...ids].map((id) => [id, hass.devices?.[id]]).filter(([, d]) => d)
    .sort((a, b) => devName(a[1]).localeCompare(devName(b[1])));
}
const devName = (d) => d?.name_by_user || d?.name || '';

class UispNmsCard extends HTMLElement {
  setConfig(config) {
    this._config = { protect: [], cycle_seconds: 5, ...config };
    this._sel = null; // selected port (interface name)
    this._armed = null; // action key waiting for the second tap
    this._busy = {}; // action key -> { k, until, done(port) }
    this._res = {}; // "clients:<port>" / "cable:<port>" -> { at, data } from the query services
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    this._resolve();
    this._tick();
  }

  connectedCallback() {
    this._key = (ev) => ev.key === 'Escape' && this._sel !== null && this._close();
    document.addEventListener('keydown', this._key);
  }

  disconnectedCallback() {
    document.removeEventListener('keydown', this._key);
  }

  _close() {
    this._sel = null;
    this._armed = null;
    this._tick();
  }

  static getStubConfig(hass) {
    const devs = uispDevices(hass);
    // Prefer a device with PoE ports, so the preview shows what the card can do.
    const poe = devs.find(([id]) => Object.values(hass.entities).some((e) => e.device_id === id && e.platform === DOMAIN && e.translation_key === 'poe'));
    return { device: (poe || devs[0] || [''])[0] };
  }

  static getConfigForm() {
    return {
      schema: [
        { name: 'device', required: true, selector: { device: { filter: { integration: DOMAIN } } } },
        { name: 'cycle_seconds', selector: { number: { min: 1, max: 120, mode: 'box', unit_of_measurement: 's' } } },
      ],
      computeLabel: (s) => ({ device: 'Device', cycle_seconds: 'PoE restart off time' }[s.name]),
      computeHelper: (s) => (s.name === 'cycle_seconds' ? 'Protect uplink ports with "protect: [1, 2]" in YAML.' : undefined),
    };
  }

  // Find the device and its entities; fill sfp from the model when the config leaves it out.
  _resolve() {
    const c = this._config;
    const h = this._hass;
    let id = c.device;
    if (id && !h.devices?.[id]) {
      id = uispDevices(h).find(([, d]) => devName(d) === c.device || d.name === c.device)?.[0];
    }
    if (!id && !c.device) id = uispDevices(h)[0]?.[0];
    this._devId = id;
    this._dev = id ? h.devices[id] : null;
    this._ents = {}; // translation_key -> [entity_id]
    for (const e of Object.values(h.entities || {})) {
      if (e.device_id !== id || e.platform !== DOMAIN || !e.translation_key) continue;
      (this._ents[e.translation_key] ||= []).push(e.entity_id);
    }
    // model_id is UISP's short model (ES-10X); model is the long name (EdgeSwitch 10X).
    const model = this._dev?.model_id || this._dev?.model || '';
    const m = model.match(/^E[SR]-(\d+)/i);
    this._sfp = c.sfp || MODEL_SFP[model] || (m && /^ES-/i.test(model) ? [+m[1] + 1, +m[1] + 2] : []);
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 'full', rows: 'auto' };
  }

  _st(id) {
    return id ? this._hass.states[id] : undefined;
  }

  _one(key) {
    return (this._ents[key] || [])[0];
  }

  _num(id) {
    const v = parseFloat(this._st(id)?.state);
    return Number.isFinite(v) ? v : null;
  }

  // Rate sensor state in bit/s, whatever unit the entity displays in.
  _bps(id) {
    const st = this._st(id);
    const v = parseFloat(st?.state);
    if (!Number.isFinite(v)) return null;
    const mul = { 'bit/s': 1, 'kbit/s': 1e3, 'Mbit/s': 1e6, 'Gbit/s': 1e9, 'B/s': 8, 'kB/s': 8e3, 'MB/s': 8e6, 'GB/s': 8e9 };
    return v * (mul[st.attributes.unit_of_measurement] ?? 1);
  }

  _fmtRate(bps, long = false) {
    if (bps === null) return '–';
    const [div, unit] = bps >= 1e9 ? [1e9, 'G'] : bps >= 1e6 ? [1e6, 'M'] : bps >= 1e3 ? [1e3, 'k'] : [1, ''];
    const v = bps / div;
    return `${v >= 100 || div === 1 ? Math.round(v) : v.toFixed(1)}${long ? ` ${unit}bit/s` : unit}`;
  }

  // Entities of one kind, keyed by the interface name they belong to.
  _byIface(key) {
    const out = {};
    for (const id of this._ents[key] || []) {
      const iface = this._st(id)?.attributes.interface;
      if (iface) out[iface] = id;
    }
    return out;
  }

  _ports() {
    const rx = this._byIface('rx_rate');
    const tx = this._byIface('tx_rate');
    const poe = this._byIface('poe');
    const en = this._byIface('port_enabled');
    const out = this._byIface('poe_output');
    const ports = [];
    for (const id of this._ents.link || []) {
      const st = this._st(id);
      const a = st?.attributes || {};
      if (!a.interface) continue;
      const k = a.interface;
      const isPort = /^port\d+$/.test(k);
      const n = a.position ?? 0;
      const sfp = this._sfp.includes(n);
      const poeSt = this._st(poe[k]);
      const outSt = this._st(out[k]);
      const enSt = this._st(en[k]);
      const up = st.state === 'on';
      ports.push({
        k, n, sfp, label: isPort ? String(n) : k, linkId: id, cableTest: a.cable_test === true,
        name: a.description || '',
        enabled: a.enabled !== false,
        up,
        speed: SPEED[a.speed] || (up ? a.speed || '' : ''),
        rawSpeed: a.speed,
        slow: up && !FAST.has(a.speed),
        stp: a.stp_state,
        poeId: poe[k], enId: enSt ? en[k] : null, outId: outSt ? out[k] : null,
        poe: poeSt && poeSt.state !== 'unavailable' ? poeSt.state : null,
        poeOutput: a.poe_output,
        poeOptions: (outSt?.attributes.options || []).filter((o) => o !== 'off'),
        watts: a.poe_power || 0,
        nativeId: a.native_vlan ?? null,
        nativeName: a.native_vlan_name || '',
        tagged: a.tagged_vlans || [],
        ctr: this._st(rx[k])?.attributes || {},
        // rx = received by the switch = the device's upload; tx = the device's download
        upBps: this._bps(rx[k]),
        downBps: this._bps(tx[k]),
      });
    }
    ports.sort((a, b) => a.n - b.n || a.k.localeCompare(b.k));
    this._collectVlans(ports);
    return ports;
  }

  // Every VLAN id on any UISP NMS port, sorted: colors are assigned in this order,
  // so a VLAN has the same color on every card.
  _allVlanIds() {
    const ids = new Set();
    for (const e of Object.values(this._hass.entities || {})) {
      if (e.platform !== DOMAIN || e.translation_key !== 'link') continue;
      const a = this._st(e.entity_id)?.attributes;
      if (!a) continue;
      if (a.native_vlan != null) ids.add(a.native_vlan);
      for (const v of a.tagged_vlans || []) ids.add(v.id);
    }
    ids.delete(1);
    return [...ids].sort((a, b) => a - b);
  }

  // VLANs used on this device's ports (VLAN 1, the default, is left out), each with a color.
  _collectVlans(ports) {
    const seen = new Map();
    for (const x of ports) {
      if (x.nativeId !== null && x.nativeId !== 1 && !seen.has(x.nativeId)) seen.set(x.nativeId, x.nativeName);
      for (const v of x.tagged) if (v.id !== 1 && (!seen.has(v.id) || !seen.get(v.id))) seen.set(v.id, v.name || seen.get(v.id) || '');
    }
    const colors = this._config.vlan_colors || {};
    const all = this._allVlanIds();
    this._vlans = [...seen].sort((a, b) => a[0] - b[0])
      .map(([id, name]) => ({ id, name, color: colors[id] || VLAN_PALETTE[Math.max(0, all.indexOf(id)) % VLAN_PALETTE.length] }));
    this._vlanColor = Object.fromEntries(this._vlans.map((v) => [v.id, v.color]));
  }

  _vlanLabel(id, name) {
    return name ? `${id} ${name}` : `VLAN ${id}`;
  }

  // Re-render, dropping busy markers whose target state arrived or whose time ran out.
  _tick() {
    const ports = this._ports();
    const now = Date.now();
    for (const [key, b] of Object.entries(this._busy)) {
      const port = ports.find((x) => x.k === b.k);
      if (now > b.until || (port && b.done(port))) delete this._busy[key];
    }
    this._render(ports);
  }

  _notify(message) {
    this.dispatchEvent(new CustomEvent('hass-notification', { bubbles: true, composed: true, detail: { message } }));
  }

  _onClick(ev) {
    const path = ev.composedPath();
    const cell = path.find((el) => el.dataset?.port);
    if (cell) {
      const k = cell.dataset.port;
      this._sel = this._sel === k ? null : k;
      this._armed = null;
      return this._tick();
    }
    if (path.some((el) => el.dataset?.close)) return this._close();
    const btn = path.find((el) => el.dataset?.act);
    if (!btn || btn.disabled) return;
    const key = btn.dataset.act;
    if (this._busy[key]) return;
    if (btn.dataset.confirm && this._armed !== key) {
      this._armed = key;
      clearTimeout(this._armTimer);
      this._armTimer = setTimeout(() => { this._armed = null; this._tick(); }, 4000);
      return this._tick();
    }
    this._armed = null;
    if (/^(clients|cable):/.test(key)) return this._query(key);
    this._run(key);
  }

  // Services that return data: the result is kept per port and shown under the ports.
  async _query(key) {
    const [act, k] = key.split(':');
    const x = this._ports().find((p) => p.k === k);
    if (!x) return;
    this._busy[key] = { k, done: () => false, until: Date.now() + 120000 };
    this._tick();
    try {
      const res = await this._hass.callWS({
        type: 'call_service', domain: DOMAIN, service: act === 'clients' ? 'get_port_clients' : 'cable_test',
        service_data: { entity_id: x.linkId }, return_response: true,
      });
      this._res[key] = { at: new Date(), data: res.response };
    } catch (err) {
      this._notify(`${x.label}: ${err.message || err}`);
    }
    delete this._busy[key];
    this._tick();
  }

  _run(key) {
    const [act, k, opt] = key.split(':');
    const x = this._ports().find((p) => p.k === k);
    if (!x) return;
    const secs = this._config.cycle_seconds;
    const plan = {
      poe_off: ['switch', 'turn_off', { entity_id: x.poeId }, (p) => p.poe === 'off', 45000],
      poe_on: ['switch', 'turn_on', { entity_id: x.poeId }, (p) => p.poe === 'on', 45000],
      poe_cycle: [DOMAIN, 'power_cycle_port', { entity_id: x.poeId, off_seconds: secs }, () => false, secs * 1000 + 12000],
      out: ['select', 'select_option', { entity_id: x.outId, option: opt }, (p) => p.poeOutput === opt, 45000],
      port_off: ['switch', 'turn_off', { entity_id: x.enId }, (p) => !p.enabled, 45000],
      port_on: ['switch', 'turn_on', { entity_id: x.enId }, (p) => p.enabled, 45000],
    }[act];
    if (!plan || !plan[2].entity_id) return;
    const [domain, service, data, done, ms] = plan;
    this._busy[key] = { k, done, until: Date.now() + ms };
    setTimeout(() => this._tick(), ms + 100);
    this._tick();
    this._hass.callService(domain, service, data).catch((err) => {
      delete this._busy[key];
      this._tick();
      this._notify(`${x.label}: ${err.message || err}`);
    });
  }

  _uptime() {
    const st = this._st(this._one('last_boot'));
    const t = Date.parse(st?.state);
    if (!Number.isFinite(t)) return '';
    const s = Math.max(0, (Date.now() - t) / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    return d ? `${d}d ${h}h` : `${h}h ${Math.floor((s % 3600) / 60)}m`;
  }

  _header(ports) {
    const dev = this._dev;
    const temp = this._num(this._one('temperature'));
    // HA converts the temperature to the user's unit; warn at 70/80 °C either way.
    const tUnit = this._st(this._one('temperature'))?.attributes.unit_of_measurement || '°C';
    const [tWarn, tCrit] = tUnit === '°F' ? [158, 176] : [70, 80];
    const connected = this._st(this._one('connected'))?.state;
    const poePorts = ports.filter((x) => x.poe !== null);
    const watts = ports.reduce((s, x) => s + x.watts, 0);
    const up = this._uptime();
    const gauge = (label, v, unit, warn, crit) => {
      const cls = v === null ? '' : v >= crit ? 'crit' : v >= warn ? 'warn' : 'ok';
      return `<div class="stat ${cls}"><span class="v">${v === null ? '–' : Math.round(v)}<small>${unit}</small></span><span class="l">${label}</span></div>`;
    };
    const meta = [dev?.model, dev?.sw_version && `fw ${dev.sw_version}`, up && `${T.up} ${up}`].filter(Boolean).map(esc).join(' · ');
    return `
      <div class="top">
        <div class="id">
          <ha-icon icon="${/^ER-/i.test(dev?.model_id || '') ? 'mdi:router-network' : 'mdi:switch'}"></ha-icon>
          <div><div class="host">${esc(devName(dev))}${connected === 'off' ? `<span class="offline">${T.offline}</span>` : ''}</div>
          <div class="meta">${meta}</div></div>
        </div>
        <div class="stats">
          ${gauge('CPU', this._num(this._one('cpu')), '%', 70, 90)}
          ${gauge('RAM', this._num(this._one('ram')), '%', 75, 90)}
          ${this._one('temperature') ? gauge(T.temp, temp, esc(tUnit), tWarn, tCrit) : ''}
          <div class="stat"><span class="v">${ports.filter((x) => x.up).length}<small>/${ports.length}</small></span><span class="l">${T.links}</span></div>
          ${poePorts.length ? `<div class="stat poe"><span class="v">${watts > 0 ? `${watts.toFixed(1)}<small> W</small>` : `${poePorts.filter((x) => x.poe === 'on').length}<small>/${poePorts.length}</small>`}</span><span class="l">${T.powered}</span></div>` : ''}
        </div>
      </div>`;
  }

  // Hover text: name, link, rates and the port's error/drop counters.
  _tip(x) {
    const c = x.ctr;
    const lines = [`${x.label}: ${x.name || '—'}`];
    lines.push(x.up ? `${x.rawSpeed} · ↓${this._fmtRate(x.downBps, true)} ↑${this._fmtRate(x.upBps, true)}` : T.noLink);
    if (c.dropped != null) lines.push(`drops ${c.dropped} · errors ${c.errors ?? 0}`);
    if (x.poe !== null) lines.push(`PoE ${x.poe === 'on' ? POE_LABEL[x.poeOutput] || x.poeOutput : 'off'}`);
    if (x.nativeId !== null) lines.push(`${T.untagged}: ${this._vlanLabel(x.nativeId, x.nativeName)}`);
    if (x.tagged.length) lines.push(`${T.tagged}: ${x.tagged.map((v) => v.id).join(', ')}`);
    return esc(lines.join('\n'));
  }

  _cell(x, bottom) {
    const busy = Object.values(this._busy).some((b) => b.k === x.k);
    const cls = [
      'cell', bottom ? 'bot' : 'upr', x.sfp ? 'sfp' : 'rj',
      x.up ? (x.slow ? 'slow' : 'up') : 'down',
      x.enabled ? '' : 'dis', x.poe === 'on' ? 'pw' : '',
      this._sel === x.k ? 'sel' : '', busy ? 'busy' : '',
    ].filter(Boolean).join(' ');
    const dots = x.tagged.filter((v) => this._vlanColor[v.id]).map((v) => `<i class="vd" style="background:${this._vlanColor[v.id]}"></i>`).join('');
    const nativeColor = this._vlanColor[x.nativeId];
    const rates = x.up && x.downBps !== null
      ? `<span class="dn-r${x.downBps ? '' : ' zero'}">↓${this._fmtRate(x.downBps)}</span><span class="up-r${x.upBps ? '' : ' zero'}">↑${this._fmtRate(x.upBps)}</span>` : '';
    const label = `
      <div class="lbl">
        <div class="nm${x.name ? '' : ' empty'}"><span>${esc(x.name) || '—'}</span></div>
        <div class="rates">${rates}</div>
      </div>`;
    // 1G is the norm, so only a slower link shows its speed (inside the jack). A disabled port is hatched with a red LED.
    const poeTag = x.poe === 'on' ? (x.watts > 0 ? `${x.watts.toFixed(1)}W` : (x.poeOutput === '24v' ? '24V' : x.poeOutput === 'active' ? '48V' : 'PoE')) : '';
    const jack = `
      <div class="jackrow">
        <span class="no">${esc(x.label)}</span>
        <div class="jack">${dots ? `<span class="dots">${dots}</span>` : ''}${poeTag ? `<span class="w">${esc(poeTag)}</span>` : ''}${x.slow ? `<span class="jspd">${esc(x.speed)}</span>` : ''}${busy ? '<ha-icon icon="mdi:loading" class="spin"></ha-icon>' : ''}</div>
        <span class="leds"><i class="l1"></i><i class="l2"></i></span>
      </div>`;
    return `<div class="${cls}${nativeColor ? ' nv' : ''}" style="--o:${x.n};${nativeColor ? `--vc:${nativeColor}` : ''}" data-port="${esc(x.k)}" title="${this._tip(x)}">${bottom ? jack + label : label + jack}</div>`;
  }

  _chassis(ports) {
    const rj = ports.filter((x) => !x.sfp);
    const sfp = ports.filter((x) => x.sfp);
    // Switches: odd ports on top, even below. Small devices (routers): one row.
    const twoRows = rj.length > 6;
    const top = twoRows ? rj.filter((x) => x.n % 2) : rj;
    const bottom = twoRows ? rj.filter((x) => !(x.n % 2)) : [];
    const cols = Math.max(top.length, bottom.length, 1);
    return `
      <div class="chassis-wrap"><div class="chassis${cols > 8 ? ' stack' : ''}">
        <div class="ports">
          <div class="rjgrid" style="--cols:${cols}">${top.map((x) => this._cell(x, false)).join('')}${bottom.map((x) => this._cell(x, true)).join('')}</div>
          ${sfp.length ? `<div class="sfpgrid">${sfp.map((x, i) => this._cell(x, twoRows && i % 2 === 1)).join('')}</div>` : ''}
        </div>
        <div class="side"><div class="sidein">${this._panel(ports.find((x) => x.k === this._sel))}</div></div>
      </div></div>`;
  }

  _btn(key, icon, text, { confirm = false, disabled = false, kind = '', title = '' } = {}) {
    const busy = !!this._busy[key];
    const armed = this._armed === key;
    const cls = ['act', kind, armed ? 'armed' : '', busy ? 'busy' : ''].filter(Boolean).join(' ');
    return `<button class="${cls}" data-act="${esc(key)}"${confirm ? ' data-confirm="1"' : ''}${disabled ? ' disabled' : ''}${title ? ` title="${esc(title)}"` : ''}>
      <ha-icon icon="${busy ? 'mdi:loading' : icon}"${busy ? ' class="spin"' : ''}></ha-icon><span>${armed ? T.again : esc(text)}</span></button>`;
  }

  _panel(x) {
    if (!x) {
      return `
        <div class="pick"><ha-icon icon="mdi:gesture-tap"></ha-icon>${T.pick}</div>
        <div class="legend">
          <span><i class="lg g"></i>${T.lgLink}</span><span><i class="lg a"></i>${T.lgSlow}</span>
          <span><i class="lg r"></i>${T.lgDis}</span><span><i class="lg a2"></i>${T.lgPoe}</span>
        </div>
        ${this._vlans.length ? `
        <div class="vlegend">
          <div class="vlh">${T.vlans}<span>${T.lgUntag} · ${T.lgTag}</span></div>
          ${this._vlans.map((v) => `<span title="VLAN ${v.id}"><i class="vsw" style="--vc:${v.color}"></i>${esc(this._vlanLabel(v.id, v.name))}</span>`).join('')}
        </div>` : ''}`;
    }
    const prot = (this._config.protect || []).map(String).includes(String(x.n)) || (this._config.protect || []).includes(x.k);
    const secs = `${this._config.cycle_seconds}s`;
    const poe = x.poe === null ? '' : `
      <div class="prow"><span class="gl">PoE${x.poe === 'on' ? `<small>${esc(x.watts > 0 ? `${x.watts.toFixed(1)}W` : POE_LABEL[x.poeOutput] || x.poeOutput)}</small>` : ''}</span>
        ${x.poe === 'on'
          ? this._btn(`poe_off:${x.k}`, 'mdi:flash-off', T.off, { confirm: true, kind: 'danger' })
          : this._btn(`poe_on:${x.k}`, 'mdi:flash', T.on, { kind: 'good' })}
        ${this._btn(`poe_cycle:${x.k}`, 'mdi:restart', `${T.restart} ${secs}`, { confirm: true, disabled: x.poe !== 'on' })}
      </div>`;
    // Changing the voltage can damage a powered device, so every choice needs a second tap.
    const outputs = x.outId && x.poeOptions.length > 1 ? `
      <div class="prow"><span class="gl">${T.output}</span>
        ${x.poeOptions.map((o) => this._btn(`out:${x.k}:${o}`, o === x.poeOutput ? 'mdi:check' : 'mdi:flash-outline', POE_SHORT[o] || o,
          { confirm: true, disabled: o === x.poeOutput, kind: o === x.poeOutput ? 'cur' : '', title: `${POE_LABEL[o] || o}: ${T.voltageWarn}` })).join('')}
      </div>` : '';
    const port = x.enId ? `
      <div class="prow"><span class="gl">${T.port}</span>
        ${x.enabled
          ? this._btn(`port_off:${x.k}`, 'mdi:lan-disconnect', T.off, { confirm: true, kind: 'danger', disabled: prot })
          : this._btn(`port_on:${x.k}`, 'mdi:lan-connect', T.on, { kind: 'good' })}
      </div>` : '';
    const c = x.ctr;
    const vdot = (id) => `<i class="vd" style="background:${this._vlanColor[id] || 'var(--fp-dim)'}"></i>`;
    const vlans = [
      ...(x.nativeId !== null ? [`${vdot(x.nativeId)}<b>${esc(this._vlanLabel(x.nativeId, x.nativeName))}</b>`] : []),
      ...x.tagged.map((v) => `${vdot(v.id)}<span title="${esc(this._vlanLabel(v.id, v.name))}">${v.id}</span>`),
    ].join(' ');
    return `
      <div class="pinfo">
      <div class="ptitle"><span class="dn">${esc(x.label)}</span><span class="pname">${esc(x.name) || `<i>${T.noName}</i>`}</span>${prot ? `<ha-icon class="shield" icon="mdi:shield-lock" title="${T.protected}"></ha-icon>` : ''}
        <button class="x" data-close="1" title="Esc"><ha-icon icon="mdi:close"></ha-icon></button></div>
      <div class="pmeta prate">${x.enabled ? (x.up ? esc(x.rawSpeed) : T.noLink) : `<b class="bad">${T.portOff}</b>`}${x.stp ? ` · STP ${esc(x.stp)}` : ''}${x.up ? `<span class="dn-r">↓${this._fmtRate(x.downBps, true)}</span><span class="up-r">↑${this._fmtRate(x.upBps, true)}</span>` : ''}</div>
      ${c.dropped != null ? `<div class="pmeta${(c.dropped || c.errors) ? ' bad' : ''}">drops ${esc(c.dropped)} · errors ${esc(c.errors ?? 0)}</div>` : ''}
      ${vlans ? `<div class="pmeta one pvl" title="${T.untagged} · ${T.tagged}">${vlans}</div>` : ''}
      </div>
      <div class="pacts">${poe}${outputs}${port}</div>`;
  }

  _len(from, to) {
    if (from == null && to == null) return '';
    return from === to || to == null ? `${from} m` : from == null ? `${to} m` : `${from}–${to} m`;
  }

  // Strip under the ports for the selected port: query buttons and their latest results.
  _tools(x) {
    if (!x) return '';
    const prot = (this._config.protect || []).map(String).includes(String(x.n)) || (this._config.protect || []).includes(x.k);
    const time = (r) => r.at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const label = (k) => this._ports().find((p) => p.k === k)?.label || k;
    let clients = '';
    const cr = this._res[`clients:${x.k}`];
    if (cr) {
      const d = cr.data || {};
      const list = d.clients || [];
      const via = d.lag ? ` · ${T.via} ${esc(d.lag)}${d.lag_ports?.length ? ` (${T.ports} ${d.lag_ports.map((k) => esc(label(k))).join(', ')})` : ''}` : '';
      clients = `
        <div class="rh"><ha-icon icon="mdi:devices"></ha-icon><b>${list.length} ${T.seen}</b><small>${time(cr)}${via}</small></div>
        ${list.length ? `<div class="cl">${list.map((c) => {
          const name = c.hostname || c.vendor || c.mac;
          const vl = c.vlan != null ? this._vlanLabel(c.vlan, c.vlan_name) : '';
          // IPv4 first; only the first address is shown, all of them in the tooltip.
          const ips = c.ip_addresses || [];
          const ordered = [...ips.filter((a) => !a.includes(':')), ...ips.filter((a) => a.includes(':'))];
          const ip = ordered.length ? `${ordered[0]}${ordered.length > 1 ? ` +${ordered.length - 1}` : ''}` : '';
          return `<div class="cr" title="${esc([c.hostname, c.vendor, c.mac, ...ordered, vl].filter(Boolean).join('\n'))}">
            <i class="vd" style="background:${this._vlanColor[c.vlan] || 'var(--fp-dim)'}"></i>
            <b>${esc(name)}</b><span>${esc(ip)}</span><span class="mac">${esc(c.mac)}</span><span>${esc(vl)}</span></div>`;
        }).join('')}</div>` : `<div class="pmeta">${T.none}</div>`}`;
    }
    let cable = '';
    const tr = this._res[`cable:${x.k}`];
    if (tr) {
      const d = tr.data || {};
      const good = (st) => /^(ok|normal|good|pass)/i.test(st || '');
      const pairs = Object.entries(d.pairs || {});
      cable = `
        <div class="rh"><ha-icon icon="mdi:cable-data"></ha-icon><b class="${good(d.status) ? 'okc' : 'bad'}">${esc(d.status ?? '?')}</b>
          <span>${esc(this._len(d.length_from, d.length_to))}</span><small>${time(tr)}</small></div>
        ${pairs.length ? `<div class="pairs">${pairs.map(([n, p]) => `<span>${T.pair} ${esc(n.toUpperCase())}
          <b class="${good(p.state) ? 'okc' : 'bad'}">${esc(p.state ?? '?')}</b> ${esc(this._len(p.length_from, p.length_to))}</span>`).join('')}</div>` : ''}
        ${d.raw ? `<pre class="raw">${esc(JSON.stringify(d.raw))}</pre>` : ''}`;
    }
    return `
      <div class="tools">
        <div class="tbtns">
          ${this._btn(`clients:${x.k}`, 'mdi:account-search', T.who)}
          ${x.cableTest ? this._btn(`cable:${x.k}`, 'mdi:cable-data', T.cable, { confirm: true, disabled: prot, title: prot ? T.cableProt : T.cableWarn }) : ''}
        </div>
        ${clients ? `<div class="res">${clients}</div>` : ''}
        ${cable ? `<div class="res">${cable}</div>` : ''}
      </div>`;
  }

  _render(ports) {
    if (!this._hass) return;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.addEventListener('click', (ev) => this._onClick(ev));
    }
    if (!this._dev) {
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:16px">${T.noDevice}</div></ha-card>`;
      return;
    }
    const scroll = this.shadowRoot.querySelector('.chassis-wrap')?.scrollLeft || 0;
    paintCard(this, `
      <style>
        ha-card { display: block; container: card / inline-size; padding: 10px 12px 12px; --accent: var(--card-accent, #26a69a); --ok: #4caf50; --slow: #ffa000; --pw: #f9a825; --bad: var(--error-color, #db4437); }
        .top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px;
                margin: -10px -12px 0; padding: 8px 12px; border-radius: var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px) 0 0;
                background: color-mix(in srgb, var(--accent) 11%, transparent); border-bottom: 2px solid color-mix(in srgb, var(--accent) 45%, transparent); }
        .id { display: flex; align-items: center; gap: 10px; min-width: 0; }
        .id ha-icon { --mdc-icon-size: 34px; color: var(--accent); }
        .host { font-size: 18px; font-weight: 600; line-height: 1.2; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
        .offline { font-size: 11px; font-weight: 700; color: #fff; background: var(--bad); padding: 1px 6px; border-radius: 4px; }
        .meta { font-size: 12px; color: var(--secondary-text-color); }
        .stats { display: flex; gap: 8px; }
        .stat { display: flex; flex-direction: column; align-items: center; min-width: 58px; padding: 6px 8px;
                border-radius: 10px; background: var(--secondary-background-color); transition: background .3s; }
        .stat .v { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.1; }
        .stat small { font-size: 11px; font-weight: 400; color: var(--secondary-text-color); margin-left: 1px; }
        .stat .l { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: var(--secondary-text-color); }
        .stat.ok { background: color-mix(in srgb, var(--ok) 14%, var(--secondary-background-color)); }
        .stat.ok .v { color: var(--ok); }
        .stat.warn { background: color-mix(in srgb, var(--slow) 22%, var(--secondary-background-color)); }
        .stat.warn .v, .stat.warn .l { color: var(--slow); }
        .stat.crit { background: var(--bad); }
        .stat.crit .v, .stat.crit .l, .stat.crit small { color: #fff; }
        .stat.poe .v { color: var(--pw); }

        /* front panel palette: light by default, dark when the HA theme is dark */
        ha-card { --fp-bg: linear-gradient(#f7f8fa, #e6e8ec); --fp-edge: inset 0 1px 0 #fff, inset 0 -2px 0 rgba(0,0,0,.08), 0 0 0 1px var(--divider-color);
                  --fp-text: #3c4043; --fp-name: #1f2124; --fp-dim: #9aa0a6; --fp-sep: #c9ccd1;
                  --fp-hover: rgba(0,0,0,.04); --fp-sel: rgba(0,0,0,.05); --fp-jack: #2b2d31; --fp-jack-edge: #8d9299; --fp-jack-up: #5f646b;
                  --fp-led-off: #c9ccd1; --fp-panel: rgba(255,255,255,.7); --fp-down: #1565c0; --fp-upl: #6a1b9a; --fp-up: #2e7d32; --fp-slow: #b26a00; }
        ha-card.dark { --fp-bg: linear-gradient(#45484e, #2c2e33 55%, #26282c); --fp-edge: inset 0 1px 0 rgba(255,255,255,.1), inset 0 -2px 0 rgba(0,0,0,.4);
                  --fp-text: #c9ccd1; --fp-name: #eceef1; --fp-dim: #6f737a; --fp-sep: #55585e;
                  --fp-hover: rgba(255,255,255,.05); --fp-sel: rgba(255,255,255,.08); --fp-jack: #101113; --fp-jack-edge: #5a5d63; --fp-jack-up: #6d7178;
                  --fp-led-off: #3a3c40; --fp-panel: rgba(0,0,0,.25); --fp-down: #64b5f6; --fp-upl: #ce93d8; --fp-up: #8fe08f; --fp-slow: #ffc95c; }
        .chassis-wrap { overflow-x: auto; border-radius: 12px; background: var(--fp-bg); box-shadow: var(--fp-edge); container-type: inline-size; margin-top: 10px; }
        .chassis { display: flex; align-items: stretch; gap: 14px; padding: 8px 10px; color: var(--fp-text); }
        .ports { display: flex; gap: 12px; margin: 0 auto; min-width: 0; }
        .side { position: relative; flex: 0 0 320px; min-height: 150px; border-radius: 10px; background: var(--fp-panel); box-shadow: inset 0 1px 3px rgba(0,0,0,.15); }
        .sidein { position: absolute; inset: 0; box-sizing: border-box; padding: 7px 12px; overflow: hidden;
                  display: flex; flex-direction: column; gap: 3px; }
        .rjgrid { display: grid; grid-template-columns: repeat(var(--cols), minmax(92px, 120px)); grid-auto-rows: auto; gap: 4px 8px; align-content: center; }
        .sfpgrid { display: grid; grid-template-rows: auto auto; gap: 2px; padding-left: 8px; border-left: 1px solid var(--fp-sep); align-content: center; }
        .cell { position: relative; display: flex; flex-direction: column; gap: 4px; padding: 5px 6px; border-radius: 8px; cursor: pointer;
                border: 1px solid transparent; transition: background .15s, border-color .15s; min-width: 0; }
        .cell.sfp { min-width: 84px; padding-left: 3px; padding-right: 3px; }
        .sfp .jackrow { gap: 4px; }
        .lbl { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
        .bot .lbl { flex-direction: column-reverse; }
        .nm { height: 16px; display: flex; align-items: center; min-width: 0; padding-right: 2px; }
        .nm span { font-size: 12.5px; line-height: 16px; font-weight: 600; color: var(--fp-name); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .nm.empty span { color: var(--fp-dim); font-weight: 400; }
        .rates { display: flex; justify-content: space-between; gap: 8px; width: 100%; height: 17px;
                 font-size: 13px; letter-spacing: -.01em; font-variant-numeric: tabular-nums; white-space: nowrap; }
        .rates .zero { opacity: .45; }
        .jspd { position: absolute; right: 3px; top: 1px; font-size: 9px; font-weight: 700; color: var(--slow); }
        .dots { position: absolute; left: 3px; top: 50%; transform: translateY(-50%); display: grid; grid-auto-flow: column; grid-template-rows: repeat(3, 6px); gap: 2px; }
        .dots .vd { width: 6px; height: 6px; }
        .jack:has(.dots) .w { margin-left: 14px; }
        .sfp .dots { grid-template-rows: repeat(2, 5px); gap: 1px 2px; left: 2px; }
        .sfp .dots .vd { width: 5px; height: 5px; }
        .vd { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex: none; }
        .nv .jack, .nv .jack::before { border-color: var(--vc) !important; }
        .nv .jack { box-shadow: inset 0 2px 4px rgba(0,0,0,.8), 0 0 0 1px var(--vc); }
        .vlegend { display: flex; flex-wrap: wrap; gap: 3px 10px; margin-top: 6px; font-size: 11px; }
        .vlh { width: 100%; display: flex; justify-content: space-between; gap: 8px; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: var(--fp-dim); }
        .vlh span { text-transform: none; letter-spacing: 0; font-weight: 400; }
        .vlegend > span { display: flex; align-items: center; gap: 5px; white-space: nowrap; }
        .vsw { width: 10px; height: 10px; border-radius: 2px; background: var(--vc); flex: none; }
        .pmeta.one { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .pvl { display: flex; align-items: center; gap: 4px; }
        .pvl b { font-weight: 600; margin-right: 6px; }
        .gl small { display: block; font-size: 9.5px; letter-spacing: 0; color: var(--pw); text-transform: none; white-space: nowrap; }
        .cell:hover { background: var(--fp-hover); }
        .cell.sel { border-color: var(--primary-color); background: var(--fp-sel); }
        .jackrow { display: flex; align-items: center; gap: 5px; }
        .no { flex: none; min-width: 17px; font-size: 12.5px; font-weight: 700; color: var(--fp-dim); text-align: right; font-variant-numeric: tabular-nums; }
        .up .no { color: var(--fp-up); } .slow .no { color: var(--fp-slow); }
        .jack { position: relative; flex: 1 1 64px; min-width: 0; max-width: 84px; height: 28px; border-radius: 3px; background: var(--fp-jack); border: 1px solid var(--fp-jack-edge);
                box-shadow: inset 0 2px 4px rgba(0,0,0,.8); display: flex; align-items: center; justify-content: center; }
        .rj .jack::before { content: ''; position: absolute; left: 50%; width: 34%; height: 5px; transform: translateX(-50%);
                            background: var(--fp-jack); border: 1px solid var(--fp-jack-edge); }
        .rj.upr .jack::before { bottom: -6px; border-top: none; border-radius: 0 0 2px 2px; }
        .rj.bot .jack::before { top: -6px; border-bottom: none; border-radius: 2px 2px 0 0; }
        .sfp .jack { height: 16px; border-radius: 2px; }
        .up .jack, .slow .jack { border-color: var(--fp-jack-up); }
        .dis .jack { background: repeating-linear-gradient(45deg, var(--fp-jack) 0 5px, #2a1414 5px 10px); border-color: #7a3a3a; }
        .w { font-size: 12px; font-weight: 700; color: var(--pw); font-variant-numeric: tabular-nums; z-index: 1; }
        .jack .spin { --mdc-icon-size: 14px; color: #fff; position: absolute; right: 2px; }
        .leds { display: flex; flex-direction: column; gap: 4px; }
        .leds i { display: block; width: 9px; height: 6px; border-radius: 1.5px; background: var(--fp-led-off); }
        .up .l1 { background: #5f5; box-shadow: 0 0 6px #5f5; }
        .slow .l1 { background: #fb3; box-shadow: 0 0 6px #fb3; }
        .dis .l1 { background: #f44; box-shadow: 0 0 6px #f44; }
        .pw .l2 { background: #fb3; box-shadow: 0 0 6px #fb3; }
        .down:not(.sel) .lbl, .down:not(.sel) .jack { opacity: .65; }

        /* query strip under the ports */
        .tools { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
        .tbtns { display: flex; flex-wrap: wrap; gap: 6px; }
        .tbtns .act { flex: 0 0 auto; }
        .res { border-radius: 10px; background: var(--fp-panel); box-shadow: inset 0 1px 3px rgba(0,0,0,.15); padding: 6px 10px; font-size: 12px; }
        .rh { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .rh ha-icon { --mdc-icon-size: 16px; color: var(--fp-dim); }
        .rh small { color: var(--fp-dim); }
        .okc { color: var(--ok); } .res b.bad { color: var(--bad); }
        .cl { margin-top: 4px; max-height: 220px; overflow-y: auto; display: grid; grid-template-columns: 10px repeat(4, minmax(0, max-content)); justify-content: start; gap: 2px 14px; align-items: center; }
        .cr { display: contents; }
        .cr > * { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .cr b { font-weight: 600; }
        .mac { font-family: var(--code-font-family, monospace); font-size: 11px; color: var(--fp-dim); }
        .pairs { display: flex; flex-wrap: wrap; gap: 2px 14px; margin-top: 4px; }
        .raw { margin: 4px 0 0; white-space: pre-wrap; word-break: break-all; font-size: 11px; }
        @container card (max-width: 600px) {
          .cl { grid-template-columns: 10px minmax(0, 1fr) minmax(0, 1fr); }
          .cr .mac, .cr span:last-child { display: none; }
        }

        /* selection panel */
        .pick { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; color: var(--fp-name); }
        .pick ha-icon { --mdc-icon-size: 20px; color: var(--fp-dim); }
        .legend { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 8px; margin-top: 6px; font-size: 11.5px; }
        .legend span { display: flex; align-items: center; gap: 8px; }
        .lg { width: 9px; height: 6px; border-radius: 1.5px; }
        .lg.g { background: #5f5; box-shadow: 0 0 5px #5f5; } .lg.a, .lg.a2 { background: #fb3; box-shadow: 0 0 5px #fb3; }
        .lg.r { background: #f44; box-shadow: 0 0 5px #f44; } .lg.a2 { outline: 1px dashed var(--fp-dim); outline-offset: 2px; }
        .ptitle { display: flex; align-items: center; gap: 8px; }
        .pname { flex: 1; min-width: 0; font-size: 15px; font-weight: 600; color: var(--fp-name); overflow-wrap: anywhere; }
        .dn { flex: none; min-width: 26px; height: 26px; padding: 0 4px; box-sizing: border-box; border-radius: 7px; display: inline-flex; align-items: center; justify-content: center;
              background: var(--primary-color); color: var(--text-primary-color, #fff); font-size: 14px; font-weight: 700; }
        .x { flex: none; border: none; background: none; padding: 2px; cursor: pointer; color: var(--fp-dim); display: flex; }
        .x ha-icon { --mdc-icon-size: 18px; }
        .pmeta { font-size: 12px; line-height: 15px; color: var(--fp-text); }
        .pmeta.bad { color: var(--bad); }
        .prate { display: flex; gap: 8px; white-space: nowrap; overflow: hidden; font-variant-numeric: tabular-nums; font-weight: 600; }
        .shield { --mdc-icon-size: 16px; color: var(--fp-dim); flex: none; }
        .dn-r { color: var(--fp-down); } .up-r { color: var(--fp-upl); }
        .bad { color: var(--bad); }
        .pacts { display: flex; flex-direction: column; gap: 3px; margin-top: auto; }
        .prow { display: flex; align-items: center; gap: 6px; }
        .gl { width: 52px; flex: none; font-size: 10px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--fp-dim); }
        .act { flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: 4px; height: 25px; padding: 0 10px;
               border-radius: 16px; border: 1px solid var(--divider-color); cursor: pointer; white-space: nowrap;
               background: var(--card-background-color); color: var(--primary-text-color); font: 600 12.5px/1 var(--ha-font-family-body, inherit); }
        .act ha-icon { --mdc-icon-size: 16px; }
        .act.danger { color: var(--bad); }
        .act.good { color: var(--ok); }
        .act.cur:disabled { opacity: 1; color: var(--pw); border-color: var(--pw); cursor: default; }
        .act.armed { color: #fff; background: var(--bad); border-color: var(--bad); }
        .act.busy { cursor: progress; }
        .act:disabled { opacity: .4; cursor: not-allowed; }
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .pinfo { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
        /* narrow card or many ports: panel goes under the ports, info left and buttons right */
        .stack { flex-direction: column; }
        .stack .side { flex: 0 0 auto; min-height: 0; }
        .stack .sidein { position: static; flex-direction: row; align-items: center; gap: 16px; }
        .stack .pinfo { flex: 1; }
        .stack .pacts { margin-top: 0; flex: none; }
        .stack .act { flex: none; min-width: 110px; }
        .stack .sidein:has(.pick) { flex-wrap: wrap; align-content: center; gap: 4px 20px; }
        .stack .sidein:has(.pick) .pick { display: none; }
        .stack .legend { display: flex; flex-wrap: wrap; gap: 3px 14px; margin-top: 0; }
        .stack .vlegend { margin-top: 0; }
        .stack .vlh { width: auto; }
        @container (max-width: 1180px) {
          .chassis { flex-direction: column; }
          .side { flex: 0 0 auto; min-height: 0; }
          .sidein { position: static; flex-direction: row; align-items: center; gap: 16px; }
          .pinfo { flex: 1; }
          .pacts { margin-top: 0; flex: none; }
          .act { flex: none; min-width: 110px; }
          .sidein:has(.pick) { flex-wrap: wrap; align-content: center; gap: 4px 20px; }
          .sidein:has(.pick) .pick { display: none; }
          .legend { display: flex; flex-wrap: wrap; gap: 3px 14px; margin-top: 0; }
          .vlegend { margin-top: 0; }
          .vlh { width: auto; }
        }
        /* small screens: never scroll sideways. Ports become a wrapping grid in port order (1, 2, 3 …),
           every port with its label above the jack; the detail panel grows instead of being fixed. */
        @container (max-width: 760px) {
          .chassis-wrap { overflow: hidden; }
          .chassis { padding: 8px; }
          .ports { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 4px 6px; margin: 0; width: 100%; }
          .rjgrid, .sfpgrid { display: contents; }
          .cell, .cell.sfp { width: auto; min-width: 0; order: var(--o); }
          .cell.bot { flex-direction: column-reverse; }
          .bot .lbl { flex-direction: column; }
          .rj.bot .jack::before { top: auto; bottom: -6px; border-top: none; border-bottom: 1px solid var(--fp-jack-edge); border-radius: 0 0 2px 2px; }
          .sidein { flex-direction: column; align-items: stretch; gap: 6px; }
          .sidein:has(.pick) { flex-direction: row; }
          .pacts { flex-direction: column; }
          .prow { flex-wrap: wrap; }
          .act { flex: 1 1 110px; width: auto; }
        }
        /* narrow card: title on its own line, stat tiles below it (wrapping, no scrolling) */
        @container card (max-width: 600px) {
          .stats { width: 100%; flex-wrap: wrap; }
          .stat { flex: 1 1 56px; min-width: 0; }
        }
      </style>
      <ha-card class="${this._hass.themes?.darkMode ? 'dark' : ''}" style="${this._config.accent ? `--card-accent:${esc(this._config.accent)}` : ''}">
        ${this._header(ports)}
        ${ports.length ? this._chassis(ports) + this._tools(ports.find((x) => x.k === this._sel)) : `<div class="meta" style="padding:12px 0 0">${T.unavailable}</div>`}
      </ha-card>`);
    const wrap = this.shadowRoot.querySelector('.chassis-wrap');
    if (wrap) wrap.scrollLeft = scroll;
  }
}

// The integration loads this file as an extra frontend module, which can run before HA installs its scoped
// custom element registry; an element defined that early gets lost. So define it once the HA app element exists.
function defineUispNmsCard() {
  if (customElements.get('uisp-nms-card')) return;
  customElements.define('uisp-nms-card', UispNmsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: 'uisp-nms-card', name: 'UISP NMS switch', description: 'Front panel of a UISP switch or router, with link, rates and PoE control', preview: true });
}
if (customElements.get('home-assistant')) defineUispNmsCard();
else customElements.whenDefined('home-assistant').then(defineUispNmsCard);
