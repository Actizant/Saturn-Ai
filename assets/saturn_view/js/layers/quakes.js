/**
 * Saturn's View -- earthquakes.
 *
 * The feed is a minute old by the time anyone sees it; the planet is not.
 * Seismic waves travel at a known speed, so for the first quarter of an hour
 * after an event the view can draw something the feed cannot contain: where
 * the ground is shaking *right now*, and where it has not arrived yet.
 *
 * Two rings, because there are two waves. The fast one (P, ~6.0 km/s) runs
 * ahead and is usually felt as a jolt; the slow one (S, ~3.5 km/s) follows and
 * is the one that does damage. Drawing both turns a dot on a map into a clock.
 */

const P_WAVE_KM_S = 6.0;
const S_WAVE_KM_S = 3.5;
const RING_WINDOW_MS = 15 * 60 * 1000;
const RING_REFRESH_MS = 500;
const RING_SEGMENTS = 72;
const RING_HEIGHT_M = 15000;
const EARTH_RADIUS_KM = 6371;

const NOTABLE_MAG = 5.0;

function ringPositions(CESIUM, lat, lon, radiusKm) {
  const positions = [];
  const angular = radiusKm / EARTH_RADIUS_KM;
  const lat1 = (lat * Math.PI) / 180;
  const lon1 = (lon * Math.PI) / 180;
  const sinD = Math.sin(angular);
  const cosD = Math.cos(angular);
  const sinLat1 = Math.sin(lat1);
  const cosLat1 = Math.cos(lat1);

  for (let i = 0; i <= RING_SEGMENTS; i++) {
    // Destination point on a sphere: the ring is the set of all places the
    // wavefront has just reached.
    const bearing = (i / RING_SEGMENTS) * Math.PI * 2;
    const lat2 = Math.asin(
      Math.max(-1, Math.min(1, sinLat1 * cosD + cosLat1 * sinD * Math.cos(bearing)))
    );
    const lon2 =
      lon1 +
      Math.atan2(
        Math.sin(bearing) * sinD * cosLat1,
        cosD - sinLat1 * Math.sin(lat2)
      );
    const lonDeg = ((((lon2 * 180) / Math.PI + 540) % 360) - 180);
    positions.push(
      CESIUM.Cartesian3.fromDegrees(lonDeg, (lat2 * 180) / Math.PI, RING_HEIGHT_M)
    );
  }

  return positions;
}

export const quakesLayer = {
  id: 'quakes',
  title: 'Earthquakes',
  group: 'earth',
  source: 'USGS',
  cadence: 60 * 1000,

  create(context) {
    const { CESIUM, scene, clock } = context;

    const points = scene.primitives.add(new CESIUM.PointPrimitiveCollection());
    const labels = scene.primitives.add(new CESIUM.LabelCollection());
    const rings = scene.primitives.add(new CESIUM.PolylineCollection());

    const gold = CESIUM.Color.fromCssColorString('#E8B84B');
    const ice = CESIUM.Color.fromCssColorString('#9FB2C8');
    const pale = CESIUM.Color.fromCssColorString('#C8D4E4');

    let quakes = [];
    const wavefronts = new Map(); // quake id -> { p, s }
    let lastRing = 0;
    let lastReadout = { at: 0, text: '' };
    let unsubscribe = null;

    function addWavefront(quake) {
      const p = rings.add({
        positions: ringPositions(CESIUM, quake.lat, quake.lon, 1),
        width: 1.2,
        material: CESIUM.Material.fromType('Color', { color: pale.withAlpha(0.35) }),
      });
      const s = rings.add({
        positions: ringPositions(CESIUM, quake.lat, quake.lon, 1),
        width: 2.0,
        material: CESIUM.Material.fromType('Color', { color: gold.withAlpha(0.85) }),
      });
      wavefronts.set(quake.id, { p, s });
    }

    function paintRings(nowMs) {
      if (nowMs - lastRing < RING_REFRESH_MS) return;
      lastRing = nowMs;

      for (const quake of quakes) {
        const ageMs = nowMs - quake.time;
        if (ageMs < 0 || ageMs > RING_WINDOW_MS) {
          const stale = wavefronts.get(quake.id);
          if (stale) {
            rings.remove(stale.p);
            rings.remove(stale.s);
            wavefronts.delete(quake.id);
          }
          continue;
        }

        const seconds = ageMs / 1000;
        let wave = wavefronts.get(quake.id);
        if (!wave) {
          addWavefront(quake);
          wave = wavefronts.get(quake.id);
        }

        wave.p.positions = ringPositions(CESIUM, quake.lat, quake.lon, P_WAVE_KM_S * seconds);
        wave.s.positions = ringPositions(CESIUM, quake.lat, quake.lon, S_WAVE_KM_S * seconds);
      }
    }

    function rebuild() {
      points.removeAll();
      labels.removeAll();

      const now = Date.now();
      let labelled = 0;

      for (const quake of quakes) {
        if (!Number.isFinite(quake.lat) || !Number.isFinite(quake.lon)) continue;

        const ageHours = (now - quake.time) / 3600000;
        // Older events fade rather than vanish: the day stays readable, but
        // nothing pretends to be happening now.
        const alpha = Math.max(0.22, 1 - ageHours / 24);
        const magnitude = Number.isFinite(quake.mag) ? quake.mag : 0;
        const notable = magnitude >= NOTABLE_MAG;

        points.add({
          position: CESIUM.Cartesian3.fromDegrees(quake.lon, quake.lat, 0),
          pixelSize: Math.max(3, 2.2 + magnitude * 2.4),
          color: (notable ? gold : ice).withAlpha(alpha),
          outlineColor: CESIUM.Color.fromCssColorString('#04060A').withAlpha(alpha * 0.8),
          outlineWidth: 1,
        });

        if (notable && labelled < 20 && quake.place) {
          labelled += 1;
          labels.add({
            position: CESIUM.Cartesian3.fromDegrees(quake.lon, quake.lat, 0),
            text: `M${magnitude.toFixed(1)}`,
            font: '10px "JetBrains Mono", Consolas, monospace',
            fillColor: gold.withAlpha(Math.max(0.45, alpha)),
            outlineColor: CESIUM.Color.fromCssColorString('#04060A'),
            outlineWidth: 3,
            style: CESIUM.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new CESIUM.Cartesian2(11, 0),
            verticalOrigin: CESIUM.VerticalOrigin.CENTER,
            horizontalOrigin: CESIUM.HorizontalOrigin.LEFT,
          });
        }
      }

      // Rings belong to events that have already started; they are built on
      // the next clock tick rather than here.
      lastRing = 0;
    }

    async function refresh() {
      const response = await fetch('/api/quakes');
      if (!response.ok) throw new Error(`USGS feed replied ${response.status}`);

      const payload = await response.json();
      const rows = Array.isArray(payload.quakes) ? payload.quakes : [];
      quakes = rows.filter((row) => Number.isFinite(row?.lat) && Number.isFinite(row?.lon));
      rebuild();

      return { count: quakes.length, source: payload.source || 'USGS' };
    }

    function readout() {
      const now = Date.now();
      if (now - lastReadout.at < 4000) return lastReadout.text;

      let biggest = 0;
      let recent = 0;
      for (const quake of quakes) {
        if (Number.isFinite(quake.mag) && quake.mag > biggest) biggest = quake.mag;
        if (now - quake.time < 3600000) recent += 1;
      }

      lastReadout = {
        at: now,
        text: quakes.length
          ? `${recent} this hour · max M${biggest.toFixed(1)}`
          : '',
      };
      return lastReadout.text;
    }

    return {
      start() {
        unsubscribe = clock.subscribe(({ now }) => paintRings(now));
      },

      refresh,

      readout,

      dispose() {
        if (unsubscribe) {
          unsubscribe();
          unsubscribe = null;
        }
        wavefronts.clear();
        scene.primitives.remove(points);
        scene.primitives.remove(labels);
        scene.primitives.remove(rings);
        quakes = [];
      },
    };
  },
};
