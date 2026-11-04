/**
 * Saturn's View -- flights layer.
 *
 * Written against our own layer contract, not adapted from theirs. The data
 * shape it reads is the OpenSky state-vector row, which is what our Python
 * provider emits and what both adsb.lol and OpenSky normalise into.
 *
 * Row slots (OpenSky order):
 *   0 icao24   1 callsign   5 lon   6 lat   7 baro_alt_m   8 on_ground
 *   9 velocity_ms   10 true_track   13 geo_alt_m   14 squawk
 */

const SLOT = {
  ICAO: 0,
  CALLSIGN: 1,
  LON: 5,
  LAT: 6,
  BARO_ALT: 7,
  ON_GROUND: 8,
  VELOCITY: 9,
  TRACK: 10,
  GEO_ALT: 13,
  SQUAWK: 14,
};

/** Altitude bands, low to high. Warm near the ground, pale at cruise. */
const BANDS = [
  { max: 0, css: '#6B7280', label: 'grounded' },
  { max: 3000, css: '#E8B84B', label: 'low' },
  { max: 9000, css: '#F2D089', label: 'mid' },
  { max: Infinity, css: '#EAF1F8', label: 'cruise' },
];

function colourFor(altitudeM, onGround, CESIUM) {
  if (onGround) return CESIUM.Color.fromCssColorString(BANDS[0].css);
  const band = BANDS.find((b) => altitudeM <= b.max) || BANDS[BANDS.length - 1];
  return CESIUM.Color.fromCssColorString(band.css);
}

function reading(row) {
  const lat = row[SLOT.LAT];
  const lon = row[SLOT.LON];
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const geo = row[SLOT.GEO_ALT];
  const baro = row[SLOT.BARO_ALT];
  return {
    icao: String(row[SLOT.ICAO] || '').trim(),
    callsign: String(row[SLOT.CALLSIGN] || '').trim(),
    lat,
    lon,
    altitude: Number.isFinite(geo) ? geo : Number.isFinite(baro) ? baro : 0,
    onGround: Boolean(row[SLOT.ON_GROUND]),
    velocity: Number(row[SLOT.VELOCITY]) || 0,
    track: Number(row[SLOT.TRACK]) || 0,
    squawk: String(row[SLOT.SQUAWK] || '').trim(),
  };
}

export const flightsLayer = {
  id: 'flights',
  title: 'Flights',
  group: 'air',
  source: 'adsb.lol',
  cadence: 4000,

  create({ CESIUM, scene, viewport }) {
    const points = new CESIUM.PointPrimitiveCollection();
    const labels = new CESIUM.LabelCollection();
    scene.primitives.add(points);
    scene.primitives.add(labels);

    const state = { aircraft: [], labelsShown: false, source: null, radiusNm: 0, lastUrl: null };

    function clear() {
      points.removeAll();
      labels.removeAll();
    }

    function draw(aircraft, cameraHeight) {
      clear();
      // Labels are the expensive half. Show them only when the camera is close
      // enough that they mean something, and never more than a readable few.
      const showLabels = cameraHeight < 2_500_000;
      const labelCap = 120;
      state.labelsShown = showLabels;

      let labelled = 0;
      for (const ac of aircraft) {
        points.add({
          position: CESIUM.Cartesian3.fromDegrees(ac.lon, ac.lat, ac.altitude),
          color: colourFor(ac.altitude, ac.onGround, CESIUM),
          pixelSize: ac.onGround ? 4 : 6,
          // Traffic is a foreground instrument, not a world inventory: the
          // marks shrink as the globe is pulled back so a 250 nm disc never
          // turns into a smear of white bubbles.
          scaleByDistance: new CESIUM.NearFarScalar(2e5, 1.35, 9e6, 0.42),
          translucencyByDistance: new CESIUM.NearFarScalar(2e5, 1.0, 9e6, 0.55),
          outlineColor: CESIUM.Color.fromCssColorString('#04060A'),
          outlineWidth: 1,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        });

        if (showLabels && labelled < labelCap && ac.callsign) {
          labels.add({
            position: CESIUM.Cartesian3.fromDegrees(ac.lon, ac.lat, ac.altitude),
            text: ac.callsign,
            font: '600 11px "Segoe UI", system-ui, sans-serif',
            fillColor: CESIUM.Color.fromCssColorString('#9FB3C8'),
            outlineColor: CESIUM.Color.fromCssColorString('#04060A'),
            outlineWidth: 2,
            style: CESIUM.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new CESIUM.Cartesian2(10, -8),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
            scale: 0.9,
          });
          labelled += 1;
        }
      }
    }

    function buildUrl() {
      const box = viewport();
      const cam = scene.camera.positionCartographic;
      // Always ask for a place, never for the whole planet. A worldwide pull is
      // slower, rate-limited upstream, and shows dots nobody can read anyway.
      const lat = box ? (box.north + box.south) / 2 : CESIUM.Math.toDegrees(cam.latitude);
      const lon = box ? (box.east + box.west) / 2 : CESIUM.Math.toDegrees(cam.longitude);
      const span = box ? Math.max(2, Math.min(box.span, 12)) : 12;
      const params = new URLSearchParams({
        lat: lat.toFixed(3),
        lon: lon.toFixed(3),
        span: span.toFixed(2),
      });
      return `/api/flights?${params.toString()}`;
    }

    return {
      async refresh() {
        const url = buildUrl();
        state.lastUrl = url;
        const response = await fetch(url, { cache: 'no-store' });
        if (!response.ok) throw new Error(`flights feed ${response.status}`);
        const payload = await response.json();
        const rows = Array.isArray(payload?.states) ? payload.states : [];

        const aircraft = [];
        for (const row of rows) {
          if (!Array.isArray(row)) continue;
          const ac = reading(row);
          if (ac) aircraft.push(ac);
        }

        state.aircraft = aircraft;
        state.source = response.headers.get('X-Flight-Source') || 'adsb.lol';
        // How far the feed actually reaches. Without it the rail would imply
        // that "26 airborne" is a world count when it is a 250 nm disc, which
        // is exactly the sort of quiet lie a freshness-first product cannot tell.
        state.radiusNm = Number(response.headers.get('X-Flight-Radius')) || 0;
        draw(aircraft, scene.camera.positionCartographic.height);

        return {
          count: aircraft.length,
          source: state.source,
          epoch: Number(payload?.time) || undefined,
        };
      },

      dispose() {
        clear();
        scene.primitives.remove(points);
        scene.primitives.remove(labels);
        state.aircraft = [];
      },

      readout() {
        const count = state.aircraft.length;
        // An empty view is information, not an absent answer. A blank row in
        // the rail reads as a broken layer; "no contacts" reads as a fact.
        if (!count) return 'no contacts';
        const airborne = state.aircraft.filter((ac) => !ac.onGround).length;
        const reach = state.radiusNm > 0 ? `${state.radiusNm} nm` : 'world';
        return `${airborne} airborne in ${reach}`;
      },

      /** Read-only access for a future HUD or selection panel. */
      get snapshot() {
        return { ...state };
      },
    };
  },
};
