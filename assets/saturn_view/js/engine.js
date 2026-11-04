/**
 * Saturn's View -- engine.
 *
 * The whole public surface of the globe lives in this file. Nothing outside
 * it touches Cesium: layers get a small context, the assistant gets
 * `window.SaturnView`, and the HUD gets events. That indirection is the point.
 *
 * There is no borrowed globe code in this tree. Cesium is the renderer; the
 * engine, the layer contract, the freshness model and the shell are Saturn's.
 */

import { createBus, CHANNELS } from './events.js';
import { createFreshness } from './freshness.js';
import { createCamera } from './camera.js';
import { createLayerRegistry } from './layers.js';
import { createLens, DEFAULT_LENS, LENS_NAMES } from './lens.js';
import { flightsLayer } from './layers/flights.js';
import { satellitesLayer } from './layers/satellites.js';
import { quakesLayer } from './layers/quakes.js';
import { cityLayer } from './layers/city.js';
import { mountHud } from './hud.js';
import { createTerrainRetryPolicy } from './terrain_retry.js';

const VERSION = '0.1.0';

/** Imagery is configuration, not a commitment. No token is ever required. */
const IMAGERY = Object.freeze({
  satellite: {
    url: 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    maximumLevel: 19,
    credit: 'Imagery: Esri, Maxar, Earthstar Geographics',
  },
});

/**
 * Terrain is free and tokenless, and it is what stops the globe looking like a
 * painted ball the moment anyone leans in. It is also the single most expensive
 * thing in the frame -- measured at roughly a third of the budget from orbit --
 * which is why the engine only asks for it when relief can actually be seen.
 *
 * The primary source is the Re:Earth / Mapterhorn ellipsoid quantized mesh, the
 * same keyless service Cesium falls back to when there is no ion token. It is
 * chosen over Esri's image server for one reason that is visible rather than
 * administrative: it answers `requestVertexNormals`, and a surface with normals
 * can catch light, which is what makes a mountain range read as rock instead
 * of as a crepe. Esri stays behind it as a fallback, because a public mesh
 * hosted by volunteers is allowed to have a bad afternoon.
 */
const TERRAIN = Object.freeze({
  primary: {
    url: 'https://terrain.reearth.land/cesium-mesh/ellipsoid',
    credit: 'Elevation: Re:Earth (CC BY 4.0)',
  },
  fallback: {
    url: 'https://elevation3d.arcgis.com/arcgis/rest/services/WorldElevation3D/Terrain3D/ImageServer',
    credit: 'Elevation: Esri',
  },
});

let instance = null;


function log(...args) {
  console.info('[saturn-view]', ...args);
}

function fail(message) {
  console.error('[saturn-view]', message);
  const boot = document.getElementById('boot');
  if (boot) {
    boot.hidden = false;
    boot.textContent = message;
    boot.classList.add('boot--error');
  }
}

function buildViewer(CESIUM, container) {
  const credits = document.getElementById('credits');

  // Cesium's own credit advertises a service this engine never calls. The
  // attribution widget itself is a licence obligation and stays; the badge for
  // an unused product does not. It has to be cleared before the renderer is
  // constructed, because the constructor clones it.
  if (CESIUM.CreditDisplay) CESIUM.CreditDisplay.cesiumCredit = undefined;

  const viewer = new CESIUM.Viewer(container, {
    // Every piece of Cesium's own UI is off. Saturn draws its own controls,
    // and a stock Cesium shell would be someone else's product on screen.
    animation: false,
    timeline: false,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    infoBox: false,
    selectionIndicator: false,
    baseLayer: false,
    terrainProvider: new CESIUM.EllipsoidTerrainProvider(),
    contextOptions: { webgl: { alpha: false, antialias: true } },
    // Attribution is a licence obligation, not decoration. It stays visible.
    creditContainer: credits || undefined,
  });

  const scene = viewer.scene;
  // Default 4x MSAA costs a third of the frame on integrated graphics and buys
  // almost nothing on a dark globe. One sample, antialias from FXAA instead.
  scene.msaaSamples = 1;
  scene.backgroundColor = CESIUM.Color.fromCssColorString('#04060A');
  scene.globe.baseColor = CESIUM.Color.fromCssColorString('#070B12');
  scene.globe.showGroundAtmosphere = true;
  scene.globe.enableLighting = false;
  scene.globe.maximumScreenSpaceError = 2;
  // The globe lights itself by altitude: unlit and fully readable from space,
  // ramping into a real day/night terminator as the camera comes down. Which
  // lens wants the sun at all is the lens's business; how far away it takes
  // effect is a property of the camera, so the band is set once here. It is the
  // same distance the lens fades its surface over, arrived at independently --
  // a shadow should already exist before there is ground worth shading.
  scene.globe.lightingFadeOutDistance = 20_000_000;
  scene.globe.lightingFadeInDistance = 9_000_000;
  scene.fog.enabled = false;
  scene.skyBox.show = false;
  // The Sun object is the light, not the picture of it: switch it off and the
  // terminator and the sheen on the ocean go with it. It is the lens that turns
  // it on. The disc and its bloom are separate, louder things and stay out of
  // the window -- a lens flare in an instrument is noise, and sunBloom in
  // particular costs a whole post-process pass to draw something nobody asked
  // for.
  scene.sun.show = false;
  scene.sunBloom = false;
  scene.moon.show = false;
  scene.screenSpaceCameraController.minimumZoomDistance = 250;
  scene.screenSpaceCameraController.maximumZoomDistance = 40_000_000;
  if (scene.postProcessStages?.fxaa) scene.postProcessStages.fxaa.enabled = true;

  // Whether imagery is fetched at all is a lens decision, not a boot decision,
  // so the engine offers providers and the lens decides what to mount. The
  // default lens mounts none: it draws its geography instead of sampling it.
  function imageryProvider(key) {
    const chosen = IMAGERY[key] || IMAGERY.satellite;
    return new CESIUM.UrlTemplateImageryProvider({
      url: chosen.url,
      maximumLevel: chosen.maximumLevel,
    });
  }

  // Belt and braces: the constructor copies the static value, so clear the
  // instance's copy as well in case a future build stops cloning. Attribution
  // for whatever imagery a lens does fetch is stated in `#credits-line`, in
  // our own type and in full -- a raw string is not a Credit, so Cesium would
  // file it as overflow and never render it.
  if (viewer.cesiumWidget?.creditDisplay) {
    viewer.cesiumWidget.creditDisplay.cesiumCredit = undefined;
  }

  // Terrain arrives asynchronously and is only *applied* when relief can be
  // seen. Holding the provider is cheap; asking it for tiles is not.
  const terrain = {
    state: 'pending',
    active: false,
    provider: null,
    credit: TERRAIN.primary.credit,
  };

  // Each source is asked in turn, and the credit follows whichever one
  // actually answered -- a line that names Re:Earth while serving Esri's
  // elevation would be a wrong attribution on screen every frame.
  //
  // The Re:Earth source is handed a `Resource` carrying a retry policy, not a
  // bare url. Re:Earth's edge throttles a zoom-out burst with HTTP 429 for a
  // few dozen tiles at once, and Cesium would mark each one FAILED and upsample
  // the parent -- so the mountains arrive blurred on the way down and never
  // sharpen. `getDerivedResource` copies the policy onto every derived tile
  // fetch, so this one Resource guards `layer.json` and every `.terrain` tile.
  // The Esri fallback stays a plain url: it is a different service with a
  // different failure mode, and the flat-terrain handoff should stay prompt.
  const logThrottle = ({ statusCode, delayMs, inWindow }) => {
    // One line per cooldown window, not per tile: a burst throttles dozens.
    if (inWindow !== 1) return;
    log(`Re:Earth terrain answered ${statusCode}; retrying throttled tiles after ${Math.round(delayMs)} ms`);
  };

  const terrainResource = () => {
    const policy = createTerrainRetryPolicy({ onThrottle: logThrottle });
    return new CESIUM.Resource({
      url: TERRAIN.primary.url,
      retryCallback: policy.retryCallback,
      retryAttempts: policy.retryAttempts,
    });
  };

  const TERRAIN_SOURCES = [
    {
      label: 'reearth',
      credit: TERRAIN.primary.credit,
      make: () =>
        CESIUM.CesiumTerrainProvider?.fromUrl(terrainResource(), {
          requestVertexNormals: true,
        }),
    },
    {
      label: 'esri',
      credit: TERRAIN.fallback.credit,
      make: () =>
        CESIUM.ArcGISTiledElevationTerrainProvider?.fromUrl(TERRAIN.fallback.url),
    },
  ];

  (function attempt(index) {
    const source = TERRAIN_SOURCES[index];
    if (!source) {
      terrain.state = 'unavailable';
      log('no terrain source answered; the globe stays flat');
      return;
    }

    let pending = null;
    try {
      pending = source.make();
    } catch (err) {
      pending = Promise.reject(err);
    }

    // A build of Cesium without the factory returns undefined rather than a
    // promise, which is a different fact from "the service is down".
    if (typeof pending?.then !== 'function') {
      terrain.state = 'unsupported';
      log(`this Cesium cannot read ${source.label} terrain`);
      return;
    }

    pending
      .then((provider) => {
        terrain.provider = provider;
        terrain.credit = source.credit;
        terrain.state = 'live';
        log(`terrain live: ${source.label}`);
      })
      .catch((err) => {
        log(`terrain ${source.label} unavailable: ${err?.message || err}`);
        attempt(index + 1);
      });
  })(0);

  // The credit line belongs to the lens, because what has to be credited
  // depends on which lens is on screen -- and the default one owes nothing to
  // an imagery service it never calls. That is not a loophole; it is the
  // consequence of drawing geography instead of licensing photographs of it.
  return { viewer, terrain, imageryProvider };
}

/**
 * Frame cost and frame rate, measured across frames that actually rendered.
 *
 * Cesium renders on demand here, so averaging over wall time is a lie twice
 * over: the gap while the globe is idle becomes one impossibly slow frame, and
 * the first average at boot divides two frames by however long startup took.
 * That is how a renderer drawing 59 frames a second came to display "0 fps".
 *
 * The median of the last forty intervals is reported rather than the mean,
 * because a single 200 ms frame -- a layer polling, a shader compiling --
 * would otherwise read as a machine that cannot draw at all. The median says
 * "this is what a frame costs when the engine is drawing", which is the
 * number an operator can act on, and it stays meaningful whether the globe is
 * idle or saturated.
 */
function buildMetrics(viewer) {
  const metrics = { fps: 0, frames: 0, primitives: 0, lastFrame: 0 };
  const IDLE_GAP = 250;
  const SAMPLE = 40;

  let previous = 0;
  const deltas = [];

  viewer.scene.postRender.addEventListener(() => {
    const now = performance.now();
    metrics.frames += 1;
    metrics.primitives = viewer.scene.primitives.length;

    if (previous) {
      const delta = now - previous;
      if (delta > 0 && delta <= IDLE_GAP) {
        deltas.push(delta);
        if (deltas.length > SAMPLE) deltas.shift();
      }
    }
    previous = now;

    if (deltas.length >= 8) {
      const sorted = deltas.slice().sort((a, b) => a - b);
      const median = sorted[sorted.length >> 1];
      metrics.lastFrame = Math.round(median * 10) / 10;
      metrics.fps = Math.round(1000 / median);
      // Exposed for the Python probe; a Promise does not round-trip in Qt.
      window.__saturnViewStats = { ...metrics, at: Date.now() };
    }
  });

  return metrics;
}

/**
 * One clock for the whole engine.
 *
 * Polled layers are driven by the registry. Animated layers need a frame tick
 * and must not each start their own loop, or a half-dozen layers each end up
 * doing work the others cannot see. Ten hertz is enough for anything that is
 * not a camera move, and cheap enough that a thousand propagated satellites
 * still leave the machine idle.
 */
function buildClock({ viewer, bus, CHANNELS, hz = 10 }) {
  const listeners = new Set();
  const period = 1000 / hz;
  let last = 0;

  viewer.scene.postRender.addEventListener(() => {
    const wall = performance.now();
    if (wall - last < period) return;
    const dt = last === 0 ? period : wall - last;
    last = wall;

    const payload = { now: Date.now(), dt };
    for (const fn of Array.from(listeners)) {
      try {
        fn(payload);
      } catch (err) {
        console.error('[saturn-view][clock] listener failed', err);
      }
    }
    bus.emit(CHANNELS.FRAME, payload);
  });

  return {
    subscribe(fn) {
      if (typeof fn !== 'function') return () => {};
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    subscribers: () => listeners.size,
  };
}

export function boot({ container = 'globe', home } = {}) {
  if (instance) return instance;

  const CESIUM = window.Cesium;
  if (!CESIUM) {
    fail('Renderer unavailable: Cesium did not load.');
    return null;
  }

  const host = document.getElementById(container);
  if (!host) {
    fail(`Missing globe container "#${container}".`);
    return null;
  }

  const bus = createBus();
  const freshness = createFreshness({ bus, CHANNELS });

  let built;
  try {
    built = buildViewer(CESIUM, host);
  } catch (err) {
    fail(`Renderer failed to start: ${err?.message || err}`);
    return null;
  }

  const { viewer, terrain, imageryProvider } = built;
  const metrics = buildMetrics(viewer);
  const clock = buildClock({ viewer, bus, CHANNELS });
  const camera = createCamera({ CESIUM, viewer, bus, CHANNELS, home });

  // Relief is only worth its cost when it can be seen. Measured at orbital
  // height it is about a third of the frame for no visible gain, so the globe
  // stays flat until the camera comes down. The two thresholds keep a camera
  // parked between them from thrashing the tile queue. The lens's handoff to a
  // photographed ground completes here, at 1.2M m, so that real terrain and
  // real imagery arrive on the same frame instead of one after the other.
  const flatTerrain = new CESIUM.EllipsoidTerrainProvider();
  const TERRAIN_NEAR = 1_200_000;
  const TERRAIN_FAR = 1_700_000;

  // Distance decides *whether* relief is worth its cost; the lens decides
  // whether relief is on the menu at all.
  let terrainAllowed = true;

  function governTerrain() {
    if (!terrain.provider) return;
    const height = camera.view().height;
    const near = terrain.active ? height <= TERRAIN_FAR : height < TERRAIN_NEAR;
    const want = terrainAllowed && near;
    if (want === terrain.active) return;
    viewer.terrainProvider = want ? terrain.provider : flatTerrain;
    terrain.active = want;
  }

  function setTerrainAllowed(value) {
    terrainAllowed = Boolean(value);
    governTerrain();
  }

  clock.subscribe(governTerrain);

  // The single object every layer receives. Deliberately small.
  const context = {
    CESIUM,
    viewer,
    scene: viewer.scene,
    bus,
    CHANNELS,
    freshness,
    camera,
    clock,
    viewport: () => camera.viewport(),
    requestRefresh: () => viewer.scene.requestRender(),
  };

  const layers = createLayerRegistry({ bus, CHANNELS, freshness, context });
  layers.register(flightsLayer);
  layers.register(satellitesLayer);
  layers.register(quakesLayer);
  // The city is the only layer whose subject is the ground itself. It draws
  // nothing above a kilometre and a half -- past that a footprint is a hairline
  // on a ball -- and its rail entry says so rather than showing a permanent
  // zero, which is a better answer than a number that means nothing.
  layers.register(cityLayer);

  // The lens is created after the layers and before the camera settles: it
  // owns the surface, the layers own the data, and neither is allowed to
  // reach into the other. Applying the stored lens here means the first frame
  // the operator sees is already the lens they left behind.
  const lens = createLens({
    CESIUM,
    viewer,
    scene: viewer.scene,
    camera,
    clock,
    bus,
    CHANNELS,
    creditsLine: document.getElementById('credits-line'),
    imageryProvider,
    setTerrainAllowed,
    imageryCredit: IMAGERY.satellite.credit,
    terrain,
    requestRefresh: () => viewer.scene.requestRender(),
  });

  lens.set(lens.name(), { announce: false, persist: false });

  camera.onMove();
  setInterval(() => freshness.tick(), 2000);

  // Stop polling when nobody is looking; resume when they are.
  document.addEventListener('visibilitychange', () => {
    layers.setPaused(document.hidden);
  });

  const api = {
    version: VERSION,
    engine: {
      viewer,
      scene: viewer.scene,
      metrics: () => ({ ...metrics }),
      terrain: () => ({ state: terrain.state, active: terrain.active, credit: terrain.credit }),
      imagery: Object.keys(IMAGERY),
      // The lens is part of the engine, not a setting of the shell: the
      // assistant changes it with the same call the rail button makes.
      lens: () => lens.name(),
      lenses: () => lens.names(),
      setLens: (name) => lens.set(name),
      cycleLens: (step) => lens.cycle(step),
      lensDescribe: () => lens.describe(),
      lensReady: () => lens.ready(),
      lensThemes: () => lens.themes(),
    },
    layers: {
      list: () => layers.list(),
      enable: (id) => layers.enable(id),
      disable: (id) => layers.disable(id),
      toggle: (id, force) => layers.toggle(id, force),
      // Asked by the host after a call it never heard back from. Answering
      // "is it on" is cheap and never in doubt, which "did the switch work"
      // cannot promise on a page that is busy building coastlines.
      isEnabled: (id) => layers.isEnabled(id),
      setEnabled: (ids, on) => layers.setEnabled(ids, on),
      refreshNow: (id) => layers.refreshNow(id),
      readout: (id) => layers.readout(id),
      snapshot: (id) => layers.snapshot(id),
      stats: () => layers.stats(),
    },
    camera: {
      view: () => camera.view(),
      viewport: () => camera.viewport(),
      flyTo: (opts) => camera.flyTo(opts),
      frame: (points, opts) => camera.frame(points, opts),
      home: (opts) => camera.home(opts),
    },
    freshness: {
      snapshot: () => freshness.snapshot(),
      subscribe: (fn) => freshness.subscribe(fn),
    },
    on: (name, fn) => bus.on(name, fn),
    setPaused: (value) => layers.setPaused(value),
    shutdown() {
      lens.dispose();
      layers.dispose();
      viewer.destroy();
      instance = null;
    },
  };

  instance = api;
  window.SaturnView = api;
  mountHud({ api });
  window.__saturnViewReady = true;

  const bootEl = document.getElementById('boot');
  if (bootEl) bootEl.hidden = true;

  camera.home({ duration: 0 });
  bus.emit(CHANNELS.READY, { version: VERSION });
  log(`engine ${VERSION} ready`);
  return api;
}

// Boot on load. The Python host waits for `window.__saturnViewReady`.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => boot());
} else {
  boot();
}
