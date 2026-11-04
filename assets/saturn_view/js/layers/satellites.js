/**
 * Saturn's View -- satellites.
 *
 * A curated shell of roughly sixteen hundred objects, propagated on this
 * machine. The feed is fetched once every six hours; the motion in between is
 * computed here, which is why this layer is never stale the way a polled feed
 * is. Nothing about a satellite's position is more than one frame old.
 *
 * The catalogue is curated rather than exhaustive on purpose. Every launch
 * adds another few hundred dots, and a globe covered in anonymous dots is a
 * worse instrument than one showing the stations, the navigation constellations,
 * the weather fleet, the visible passes and the geostationary belt.
 *
 * The ground track is drawn in two halves: the arc behind the satellite is
 * memory, the arc ahead of it is prediction. That distinction is the whole
 * thesis of the product, drawn as a line.
 */

import {
  parseTles,
  propagate,
  toGeodetic,
  groundTrack,
  nextPasses,
  orbitClass,
} from '../lib/orbit.js';

const GROUP = 'catalog';
const HERO_PREFERENCE = ['25544', '48274', '20580']; // ISS, Tiangong, Hubble
const TRACK_MINUTES = 46;
const TRACK_STEP_SEC = 60;
const TRACK_HEIGHT_M = 12000;
const TRACK_REFRESH_MS = 2000;
const READOUT_MS = 5000;

/**
 * Coarse classes get two colours: the geostationary belt is gold, the rest ice.
 * They are deliberately pale. Against daytime cloud and desert a mid-grey dot
 * vanishes; the contrast has to come from the dark underlay, not from the dot.
 */
const CLASS_COLOUR = {
  leo: '#D3E0F0',
  meo: '#F6FAFE',
  geo: '#E8B84B',
  heo: '#DCE7F4',
};

/**
 * One mark, drawn once, reused sixteen hundred times.
 *
 * A WebGL point is a hard-edged square of colour with no antialiasing worth
 * the name, and `outlineWidth` is silently ignored on Windows -- so a plain
 * point over sunlit desert either disappears or, with a flat dark disc behind
 * it, reads as a black donut. A radial gradient does what the point cannot:
 * a bright core that fades into a soft shadow, which stays legible over ice,
 * cloud, sand and open ocean alike.
 *
 * Built on a canvas so there is still no binary asset in the tree.
 */
function starSprite() {
  const size = 64;
  const centre = size / 2;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const g = canvas.getContext('2d');

  const skirt = g.createRadialGradient(centre, centre, 0, centre, centre, centre);
  skirt.addColorStop(0, 'rgba(4, 6, 10, 0.92)');
  skirt.addColorStop(0.62, 'rgba(4, 6, 10, 0.58)');
  skirt.addColorStop(1, 'rgba(4, 6, 10, 0)');
  g.fillStyle = skirt;
  g.beginPath();
  g.arc(centre, centre, centre, 0, Math.PI * 2);
  g.fill();

  const core = g.createRadialGradient(centre, centre, 0, centre, centre, centre * 0.62);
  core.addColorStop(0, 'rgba(255, 255, 255, 1)');
  core.addColorStop(0.45, 'rgba(255, 255, 255, 0.94)');
  core.addColorStop(1, 'rgba(255, 255, 255, 0)');
  g.fillStyle = core;
  g.beginPath();
  g.arc(centre, centre, centre * 0.62, 0, Math.PI * 2);
  g.fill();

  return canvas;
}

export const satellitesLayer = {
  id: 'satellites',
  title: 'Satellites',
  group: 'orbit',
  source: 'CelesTrak',
  // Element sets move by metres per day. Six hours is already generous; the
  // freshness bands scale with this, so the layer is never unfairly "stale".
  cadence: 6 * 60 * 60 * 1000,

  create(context) {
    const { CESIUM, scene, camera, viewport, clock } = context;

    const sprite = starSprite();
    const stars = scene.primitives.add(new CESIUM.BillboardCollection({ scene }));
    const heroStars = scene.primitives.add(new CESIUM.BillboardCollection({ scene }));
    const labels = scene.primitives.add(new CESIUM.LabelCollection());
    const lines = scene.primitives.add(new CESIUM.PolylineCollection());

    const gold = CESIUM.Color.fromCssColorString('#E8B84B');
    const memory = CESIUM.Color.fromCssColorString('#8C9AAA');
    const prophecy = CESIUM.Color.fromCssColorString('#F2D089');

    // The hero is the same mark, three times the size and lit from the same
    // gold as the accent. It is depth-tested like everything else: a satellite
    // on the far side of the planet must not be drawn on top of it, or the
    // globe stops being a globe and becomes a wallpaper.
    const heroStar = heroStars.add({
      position: new CESIUM.Cartesian3(),
      image: sprite,
      width: 26,
      height: 26,
      color: gold,
      show: false,
    });

    const heroLabel = labels.add({
      position: new CESIUM.Cartesian3(),
      text: '',
      font: '600 12px "Cascadia Mono", Consolas, ui-monospace, monospace',
      fillColor: CESIUM.Color.fromCssColorString('#F2D089'),
      outlineColor: CESIUM.Color.fromCssColorString('#04060A'),
      // A wide outline swallows a small glyph whole; two pixels separates the
      // gold from bright terrain without eating the type.
      outlineWidth: 2,
      style: CESIUM.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new CESIUM.Cartesian2(0, -20),
      verticalOrigin: CESIUM.VerticalOrigin.BOTTOM,
      show: false,
    });

    const trackLines = [];
    const classColours = {};
    for (const [key, css] of Object.entries(CLASS_COLOUR)) {
      classColours[key] = CESIUM.Color.fromCssColorString(css);
    }

    let sats = [];
    let entries = [];
    let hero = null;
    let lastPaint = 0;
    let lastTrack = 0;
    let readoutAt = 0;
    let readoutText = '';
    let unsubscribe = null;
    let trackInfo = null;

    /* -- layout ---------------------------------------------------------- */

    function pickHero() {
      for (const norad of HERO_PREFERENCE) {
        const found = entries.find((entry) => entry.sat.norad === norad);
        if (found) return found;
      }
      return entries[0] || null;
    }

    function rebuild() {
      stars.removeAll();
      entries = new Array(sats.length);

      for (let i = 0; i < sats.length; i++) {
        const sat = sats[i];
        const position = new CESIUM.Cartesian3();
        entries[i] = {
          sat,
          position,
          local: { x: 0, y: 0, z: 0 },
          star: stars.add({
            position,
            image: sprite,
            width: 8,
            height: 8,
            color: classColours[orbitClass(sat)],
            scaleByDistance: new CESIUM.NearFarScalar(5e5, 1.5, 3e7, 0.7),
            translucencyByDistance: new CESIUM.NearFarScalar(5e5, 1.0, 3e7, 0.62),
          }),
        };
      }

      hero = pickHero();
      heroStar.show = Boolean(hero);
      heroLabel.show = Boolean(hero);
      if (hero) heroLabel.text = hero.sat.name || `NORAD ${hero.sat.norad}`;

      readoutAt = 0;
      clearTrack();
    }

    /* -- ground track ---------------------------------------------------- */

    function clearTrack() {
      for (const line of trackLines) line.show = false;
    }

    function lineAt(index) {
      while (trackLines.length <= index) {
        trackLines.push(
          lines.add({
            positions: [],
            width: 1.5,
            material: CESIUM.Material.fromType('Color', { color: memory }),
            show: true,
          })
        );
      }
      return trackLines[index];
    }

    /** A track that crosses the date line must be cut, or the chord runs through Earth. */
    function splitAtAntimeridian(track) {
      const out = [];
      let current = [];
      let previousLon = null;

      for (const point of track) {
        if (previousLon !== null && Math.abs(point.lon - previousLon) > 180) {
          if (current.length > 1) out.push(current);
          current = [];
        }
        current.push(point);
        previousLon = point.lon;
      }
      if (current.length > 1) out.push(current);
      return out;
    }

    function updateTrack(nowMs) {
      if (!hero) return;

      // A track drawn to a satellite on the far side of the planet is a gold
      // stroke with nothing at the end of it, entering over the limb and then
      // stopping in open ocean. Hide it until the object is actually in view:
      // that is the difference between an instrument and a decoration.
      const Occluder = CESIUM.EllipsoidalOccluder;
      if (Occluder) {
        const horizon = new Occluder(scene.globe.ellipsoid, scene.camera.positionWC);
        if (!horizon.isPointVisible(hero.position)) {
          clearTrack();
          trackInfo = { at: nowMs, visible: false };
          return;
        }
      }

      const past = groundTrack(hero.sat, nowMs - TRACK_MINUTES * 60000, nowMs, TRACK_STEP_SEC);
      const ahead = groundTrack(hero.sat, nowMs, nowMs + TRACK_MINUTES * 60000, TRACK_STEP_SEC);

      const segments = [];
      for (const piece of splitAtAntimeridian(past)) {
        segments.push({ piece, color: memory, width: 1.2 });
      }
      for (const piece of splitAtAntimeridian(ahead)) {
        // The future half is drawn heavier than the past half. A track that is
        // uniform along its length says nothing; a track that thickens ahead of
        // the satellite is a sentence you can read at a glance.
        segments.push({ piece, color: prophecy, width: 2.2 });
      }

      trackInfo = {
        at: nowMs,
        visible: true,
        pastPoints: past.length,
        aheadPoints: ahead.length,
        segments: segments.map((s) => s.piece.length),
        // The first predicted point *is* the satellite. If these two numbers
        // disagree the track is not describing the object it is drawn for.
        aheadStart: ahead.length ? ahead[0] : null,
      };

      for (let i = 0; i < segments.length; i++) {
        const line = lineAt(i);
        line.positions = segments[i].piece.map((p) =>
          CESIUM.Cartesian3.fromDegrees(p.lon, p.lat, TRACK_HEIGHT_M)
        );
        line.material = CESIUM.Material.fromType('Color', { color: segments[i].color });
        line.width = segments[i].width;
        line.show = true;
      }
      for (let i = segments.length; i < trackLines.length; i++) trackLines[i].show = false;
    }

    /* -- painting -------------------------------------------------------- */

    function paint(nowMs) {
      if (!entries.length) return;

      // Only work as hard as the eye can see. Zoomed out, a 3.8 km step is a
      // tenth of a pixel; zoomed in, it is the difference between smooth and
      // stuttering.
      const height = camera.view().height;
      const interval = height > 4e6 ? 500 : height > 1.2e6 ? 200 : 100;
      if (nowMs - lastPaint < interval) return;
      lastPaint = nowMs;

      for (let i = 0; i < entries.length; i++) {
        const entry = entries[i];
        const p = propagate(entry.sat, nowMs, entry.local);
        entry.position.x = p.x * 1000;
        entry.position.y = p.y * 1000;
        entry.position.z = p.z * 1000;
        // The collections clone whatever vector they are handed, so the
        // position has to be handed over each frame rather than mutated in
        // place.
        entry.star.position = entry.position;
      }

      if (hero) {
        heroStar.position = hero.position;
        heroLabel.position = hero.position;
      }

      if (nowMs - lastTrack > TRACK_REFRESH_MS) {
        lastTrack = nowMs;
        updateTrack(nowMs);
      }
    }

    /* -- refresh --------------------------------------------------------- */

    async function refresh() {
      const response = await fetch(`/api/celestrak/${GROUP}`);
      if (!response.ok) throw new Error(`CelesTrak replied ${response.status}`);

      const parsed = parseTles(await response.text());
      if (parsed.length === 0) throw new Error('element set came back empty');

      sats = parsed;
      rebuild();
      return { count: sats.length, source: 'CelesTrak' };
    }

    /* -- readout --------------------------------------------------------- */

    /**
     * The one number a person actually wants: when does this thing come over
     * the place I am looking at. Computed against the viewport centre, cached
     * for five seconds because the HUD asks every second.
     */
    function readout() {
      if (!hero) return '';
      const now = Date.now();
      if (now - readoutAt < READOUT_MS) return readoutText;
      readoutAt = now;

      const box = viewport();
      const view = camera.view();
      const lat = box ? (box.north + box.south) / 2 : view.lat;
      const lon = box ? (box.east + box.west) / 2 : view.lon;

      const passes = nextPasses(hero.sat, lat, lon, {
        fromMs: now,
        hours: 24,
        minElevation: 10,
        stepSec: 20,
        maxPasses: 1,
      });

      const name = hero.sat.name || hero.sat.norad;
      if (!passes.length) {
        readoutText = `${name} no pass 24h`;
        return readoutText;
      }

      const minutes = Math.round((passes[0].aos - now) / 60000);
      readoutText = minutes <= 0 ? `${name} overhead` : `${name} in ${minutes}m`;
      return readoutText;
    }

    /* -- lifecycle ------------------------------------------------------- */

    return {
      start() {
        unsubscribe = clock.subscribe(({ now }) => paint(now));
      },

      refresh,

      readout,

      /** Read-only state: what is being shown, and where the track claims to be. */
      get snapshot() {
        if (!hero) return { count: sats.length, hero: null, heroAt: null, track: trackInfo };
        const geo = toGeodetic({
          x: hero.position.x / 1000,
          y: hero.position.y / 1000,
          z: hero.position.z / 1000,
        });
        return {
          count: sats.length,
          hero: { norad: hero.sat.norad, name: hero.sat.name },
          heroAt: { lat: Number(geo.lat.toFixed(3)), lon: Number(geo.lon.toFixed(3)) },
          track: trackInfo,
        };
      },

      dispose() {
        if (unsubscribe) {
          unsubscribe();
          unsubscribe = null;
        }
        clearTrack();
        heroStar.show = false;
        heroLabel.show = false;
        scene.primitives.remove(stars);
        scene.primitives.remove(heroStars);
        scene.primitives.remove(labels);
        scene.primitives.remove(lines);
        entries = [];
        sats = [];
        hero = null;
      },
    };
  },
};
