/**
 * Saturn's View -- readout and layer rail.
 *
 * Two jobs, both small. The status line reports the weakest enabled layer,
 * because a view is only as current as its oldest eye. The rail lists every
 * layer the engine knows about with its own freshness, and lets the operator
 * switch it on or off without touching the globe.
 *
 * Nothing here reaches into a layer or the renderer. Everything comes from the
 * public API, which is the same API the assistant will use.
 */

const POLL_MS = 900;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = text;
  return element;
}

function buildRail(api, rail, list, repaint) {
  const rows = new Map();

  rail.hidden = false;
  for (const layer of api.layers.list()) {
    const item = node('li', 'rail__item');
    item.dataset.id = layer.id;
    item.dataset.group = layer.group;
    const button = node('button', 'rail__button');
    button.type = 'button';
    button.title = `${layer.title} -- ${layer.source || 'source unknown'} -- every ${Math.round(
      layer.cadence / 1000
    )}s`;

    const dot = node('span', 'rail__dot');
    const name = node('span', 'rail__name', layer.title);
    const figure = node('span', 'rail__figure');
    const age = node('span', 'rail__age', 'off');

    button.append(dot, name, figure, age);
    button.addEventListener('click', () => {
      api.layers.toggle(layer.id);
      repaint();
    });

    item.append(button);
    list.append(item);
    rows.set(layer.id, { item, button, dot, figure, age });
  }

  return rows;
}

/**
 * The lens switcher. Three words, no dropdown, no menu: a lens is a way of
 * seeing, not a preference filed away under settings. Whichever one is on is
 * the only one lit.
 *
 * Button titles come from the engine's own description of each lens, so the
 * words here and the words Saturn speaks cannot drift apart.
 */
function buildLensSwitcher(api, rail, list, repaint) {
  const names = api.engine.lenses ? api.engine.lenses() : [];
  if (!names || names.length < 2) return new Map();

  const themes = api.engine.lensThemes ? api.engine.lensThemes() : {};
  const wrap = node('div', 'rail__lens');
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', 'Lens');

  const buttons = new Map();
  for (const name of names) {
    const button = node('button', 'rail__lensbutton', name.toUpperCase());
    button.type = 'button';
    button.title = `${name} -- ${themes[name] || 'lens'}`;
    button.addEventListener('click', () => {
      api.engine.setLens(name);
      repaint();
    });
    wrap.append(button);
    buttons.set(name, button);
  }

  const head = rail.querySelector('.rail__head');
  if (head) head.after(wrap);
  else rail.insertBefore(wrap, list);

  return buttons;
}

export function mountHud({ api }) {
  const status = document.getElementById('status');
  const rail = document.getElementById('rail');
  const railList = document.getElementById('rail-list');
  const fpsEl = document.getElementById('status-fps');
  const liveEl = document.getElementById('status-live');

  if (status) {
    status.hidden = false;
    const version = document.getElementById('status-version');
    if (version) version.textContent = `v${api.version}`;
  }

  const rows = rail && railList ? buildRail(api, rail, railList, () => paint()) : new Map();
  const lensButtons =
    rail && railList ? buildLensSwitcher(api, rail, railList, () => paint()) : new Map();

  function paint() {
    const metrics = api.engine.metrics();
    // Before a half-second of real drawing has been measured there is no
    // frame rate yet. "0 fps" was a reading of nothing; a dash is the truth.
    if (fpsEl) fpsEl.textContent = metrics.fps ? `${metrics.fps} fps` : '\u2014';

    if (lensButtons.size && api.engine.lens) {
      const active = api.engine.lens();
      for (const [name, button] of lensButtons) {
        const on = name === active;
        button.classList.toggle('is-on', on);
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
    }

    const layers = api.layers.list();
    let worst = null;

    for (const layer of layers) {
      const row = rows.get(layer.id);
      if (!row) continue;

      const fresh = layer.freshness || {};
      const state = layer.enabled ? fresh.state || 'off' : 'off';

      row.item.classList.toggle('is-on', layer.enabled);
      row.button.setAttribute('aria-pressed', layer.enabled ? 'true' : 'false');
      row.dot.className = `rail__dot is-${state}`;
      row.age.textContent = layer.enabled ? fresh.age ?? '--' : 'off';
      row.figure.textContent = layer.enabled ? layer.readout || '' : '';

      if (layer.enabled && Number.isFinite(fresh.ageMs) && (!worst || fresh.ageMs > worst.ageMs)) {
        worst = { ...fresh, ageMs: fresh.ageMs, title: layer.title };
      }
    }

    if (!liveEl) return;

    if (!worst) {
      liveEl.textContent = layers.some((l) => l.enabled) ? 'settling' : 'no layers';
      liveEl.className = '';
      return;
    }

    liveEl.textContent = `${worst.title.toLowerCase()} ${worst.age}`;
    liveEl.className = `is-${worst.state}`;
  }

  paint();
  const timer = setInterval(paint, POLL_MS);

  return () => clearInterval(timer);
}
