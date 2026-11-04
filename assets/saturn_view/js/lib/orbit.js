/**
 * Saturn's View -- orbit.
 *
 * Two-line elements in, Earth-fixed position out. No library, no WASM, no CDN:
 * this is ~200 lines of first-order theory and it is the only maths in the
 * engine that has to be *right* rather than pretty.
 *
 * Model: Brouwer mean elements with J2 secular rates on the node, the argument
 * of perigee and the mean anomaly. The TLE's mean motion is Kozai-style, so it
 * is de-Kozai'd into a Brouwer rate before anything is integrated -- skipping
 * that step costs about 25 km per day of along-track drift, which is exactly
 * the kind of quiet wrongness that makes a globe untrustworthy.
 *
 * What this is *not*: full SGP4. The short-period terms are omitted, which is
 * worth a few kilometres in low orbit. On a globe read at a pixel per ~30 km
 * that is invisible; if we ever claim metre-level truth, SGP4 belongs here.
 *
 * Frames: TLE elements are TEME. Output is WGS-84 Earth-fixed, in kilometres,
 * which is the same frame Cesium's Cartesian3 uses -- multiply by 1000 and the
 * vector can be handed to the renderer untouched.
 */

const MU = 398600.4418; // km^3/s^2, WGS-84 gravitational parameter
const RE = 6378.137; // km, WGS-84 semi-major axis
const J2 = 1.08262668e-3; // WGS-84 second zonal harmonic
const E2 = 6.69437999014e-3; // WGS-84 first eccentricity squared
const DEG = Math.PI / 180;
const TWO_PI = Math.PI * 2;

/* -- element parsing ------------------------------------------------------ */

/** TLE epoch is a two-digit year plus a fractional day of year. */
function epochToMs(twoDigitYear, dayOfYear) {
  const year = twoDigitYear < 57 ? 2000 + twoDigitYear : 1900 + twoDigitYear;
  return Date.UTC(year, 0, 1) + (dayOfYear - 1) * 86400000;
}

/**
 * Parse a CelesTrak element set (name / line 1 / line 2 repeating).
 *
 * Column positions are the ones in the NORAD specification, 1-based and
 * inclusive, hence the slice offsets. Nothing is trusted: a line that does not
 * yield a finite orbit is dropped rather than drawn at the origin.
 */
export function parseTles(text) {
  const lines = String(text || '').split(/\r?\n/);
  const out = [];

  for (let i = 0; i < lines.length - 1; i++) {
    const l1 = lines[i];
    const l2 = lines[i + 1];
    if (l1?.[0] !== '1' || l2?.[0] !== '2') continue;

    const previous = lines[i - 1];
    const name =
      previous && previous[0] !== '1' && previous[0] !== '2' && previous.trim()
        ? previous.trim()
        : null;

    const year = Number.parseInt(l1.slice(18, 20), 10);
    const dayOfYear = Number.parseFloat(l1.slice(20, 32));
    const inclination = Number.parseFloat(l2.slice(8, 16));
    const raan = Number.parseFloat(l2.slice(17, 25));
    const ecc = Number.parseFloat(`0.${l2.slice(26, 33).trim()}`);
    const argPerigee = Number.parseFloat(l2.slice(34, 42));
    const meanAnomaly = Number.parseFloat(l2.slice(43, 51));
    const revPerDay = Number.parseFloat(l2.slice(52, 63));

    if (
      !Number.isFinite(dayOfYear) ||
      !Number.isFinite(inclination) ||
      !Number.isFinite(raan) ||
      !Number.isFinite(ecc) ||
      !Number.isFinite(argPerigee) ||
      !Number.isFinite(meanAnomaly) ||
      !Number.isFinite(revPerDay) ||
      revPerDay <= 0
    ) {
      continue;
    }

    out.push({
      norad: l2.slice(2, 7).trim(),
      name,
      epochMs: epochToMs(year, dayOfYear),
      inclination: inclination * DEG,
      raan: raan * DEG,
      ecc,
      argPerigee: argPerigee * DEG,
      meanAnomaly: meanAnomaly * DEG,
      revPerDay,
    });

    i += 1; // line 2 is consumed with line 1
  }

  return out;
}

/* -- derived quantities --------------------------------------------------- */

/**
 * Everything that depends only on the element set, computed once and cached
 * on the element object. Called tens of thousands of times per second, so it
 * must not allocate after the first call.
 */
function derive(o) {
  if (o._d) return o._d;

  const n0 = (o.revPerDay * TWO_PI) / 86400; // rad/s, Kozai mean motion
  const e = o.ecc;
  const cosI = Math.cos(o.inclination);
  const cos2 = cosI * cosI;
  const k2 = 0.5 * J2 * RE * RE;
  const oneMinusE2 = 1 - e * e;

  // SGP4's de-Kozai step, in kilometres instead of earth radii.
  const a1 = Math.cbrt(MU / (n0 * n0));
  const d1 = (1.5 * k2 * (3 * cos2 - 1)) / (a1 * a1 * Math.pow(oneMinusE2, 1.5));
  const a2 = a1 * (1 - d1 * (1 / 3 + d1 * (1 + (134 / 81) * d1)));
  const d0 = (1.5 * k2 * (3 * cos2 - 1)) / (a2 * a2 * Math.pow(oneMinusE2, 1.5));
  const n = n0 / (1 + d0);

  const a = Math.cbrt(MU / (n * n));
  const p = a * oneMinusE2;
  const q = (RE / p) * (RE / p);

  const d = {
    a,
    e,
    n,
    cosI,
    sinI: Math.sin(o.inclination),
    raanDot: -1.5 * n * J2 * q * cosI,
    argpDot: 0.75 * n * J2 * q * (5 * cos2 - 1),
    mDot: n * (1 + 0.75 * J2 * q * Math.sqrt(oneMinusE2) * (3 * cos2 - 1)),
    periodSec: TWO_PI / n,
    apogeeKm: a * (1 + e) - RE,
    perigeeKm: a * (1 - e) - RE,
  };

  o._d = d;
  return d;
}

/* -- time ----------------------------------------------------------------- */

/** Julian date from a Unix millisecond stamp. */
function julianDate(ms) {
  return ms / 86400000 + 2440587.5;
}

/**
 * Greenwich mean sidereal time, IAU-1982 expression, returned in radians.
 * This is the whole of the TEME -> Earth-fixed rotation; polar motion is
 * sub-metre and not worth a table.
 */
function gmst(ms) {
  const T = (julianDate(ms) - 2451545.0) / 36525;
  const seconds =
    67310.54841 +
    (876600 * 3600 + 8640184.812866) * T +
    0.093104 * T * T -
    6.2e-6 * T * T * T;
  const degrees = (seconds / 240) % 360;
  return ((degrees % 360) + 360) % 360 * DEG;
}

/* -- propagation ---------------------------------------------------------- */

/**
 * Inertial position at `ms`, in kilometres.
 * Writes into `out` so a caller painting thousands of satellites per frame
 * does not create thousands of short-lived objects.
 */
export function propagateEci(o, ms, out = { x: 0, y: 0, z: 0 }) {
  const d = derive(o);
  const dt = (ms - o.epochMs) / 1000;

  const raan = o.raan + d.raanDot * dt;
  const argp = o.argPerigee + d.argpDot * dt;
  const mean = o.meanAnomaly + d.mDot * dt;
  const M = ((mean % TWO_PI) + TWO_PI) % TWO_PI;

  // Kepler's equation, Newton-Raphson. Eight passes is generous; it converges
  // in three for every orbit below e = 0.9.
  let E = M;
  for (let i = 0; i < 8; i++) {
    const step = (E - d.e * Math.sin(E) - M) / (1 - d.e * Math.cos(E));
    E -= step;
    if (Math.abs(step) < 1e-10) break;
  }

  const cosE = Math.cos(E);
  const sinE = Math.sin(E);
  const r = d.a * (1 - d.e * cosE);
  const nu = Math.atan2(Math.sqrt(1 - d.e * d.e) * sinE, cosE - d.e);
  const u = argp + nu;

  const cu = Math.cos(u);
  const su = Math.sin(u);
  const co = Math.cos(raan);
  const so = Math.sin(raan);

  out.x = r * (cu * co - su * d.cosI * so);
  out.y = r * (cu * so + su * d.cosI * co);
  out.z = r * su * d.sinI;
  return out;
}

/** Earth-fixed position at `ms`, in kilometres (the frame Cesium wants). */
export function propagate(o, ms, out = { x: 0, y: 0, z: 0 }) {
  const eci = propagateEci(o, ms, out);
  const g = gmst(ms);
  const cg = Math.cos(g);
  const sg = Math.sin(g);
  const x = eci.x;
  const y = eci.y;

  eci.x = x * cg + y * sg;
  eci.y = -x * sg + y * cg;
  return eci;
}

/** Sub-satellite point and altitude, from an Earth-fixed position. */
export function toGeodetic(pos) {
  const radial = Math.hypot(pos.x, pos.y);
  const r = Math.hypot(pos.x, pos.y, pos.z);

  if (radial < 1e-6) {
    // Over a pole the longitude is undefined and the altitude is trivial.
    return { lat: pos.z >= 0 ? 90 : -90, lon: 0, alt: Math.abs(pos.z) - RE };
  }

  let lat = Math.atan2(pos.z, radial * (1 - E2));
  let N = RE;
  for (let i = 0; i < 4; i++) {
    const s = Math.sin(lat);
    N = RE / Math.sqrt(1 - E2 * s * s);
    lat = Math.atan2(pos.z + E2 * N * s, radial);
  }

  const s = Math.sin(lat);
  N = RE / Math.sqrt(1 - E2 * s * s);

  return {
    lat: lat / DEG,
    lon: Math.atan2(pos.y, pos.x) / DEG,
    alt: radial / Math.cos(lat) - N,
  };
}

/** Position on the ellipsoid, plus the local up vector, for look angles. */
function observerVector(latDeg, lonDeg, heightKm = 0) {
  const lat = latDeg * DEG;
  const lon = lonDeg * DEG;
  const s = Math.sin(lat);
  const c = Math.cos(lat);
  const N = RE / Math.sqrt(1 - E2 * s * s);
  const cl = Math.cos(lon);
  const sl = Math.sin(lon);

  return {
    x: (N + heightKm) * c * cl,
    y: (N + heightKm) * c * sl,
    z: (N * (1 - E2) + heightKm) * s,
    up: { x: c * cl, y: c * sl, z: s },
  };
}

/** Elevation of the satellite above the observer's horizon, in degrees. */
function elevationAt(o, observer, ms, scratch) {
  const p = propagate(o, ms, scratch);
  const dx = p.x - observer.x;
  const dy = p.y - observer.y;
  const dz = p.z - observer.z;
  const range = Math.hypot(dx, dy, dz);
  if (range < 1e-6) return 90;

  const sinEl = (dx * observer.up.x + dy * observer.up.y + dz * observer.up.z) / range;
  return Math.asin(Math.max(-1, Math.min(1, sinEl))) / DEG;
}

/**
 * The next few visible passes over a ground point.
 *
 * This is the whole reason the layer is worth having. A globe that can only
 * show where a satellite *is* is a picture; one that can say "it comes over
 * you in eleven minutes, 63 degrees up" is an instrument. The answer costs a
 * few thousand cheap propagations, so it is computed on demand, not polled.
 */
export function nextPasses(
  o,
  lat,
  lon,
  { fromMs = Date.now(), hours = 12, minElevation = 10, stepSec = 30, maxPasses = 4 } = {}
) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return [];

  const observer = observerVector(lat, lon);
  const scratch = { x: 0, y: 0, z: 0 };
  const stepMs = Math.max(5, stepSec) * 1000;
  const end = fromMs + hours * 3600 * 1000;
  const passes = [];

  let open = null;
  let previous = null;

  for (let t = fromMs; t <= end; t += stepMs) {
    const el = elevationAt(o, observer, t, scratch);

    if (el >= minElevation) {
      if (!open) {
        open = { aos: previous ?? t, peak: t, peakEl: el };
      } else if (el > open.peakEl) {
        open.peak = t;
        open.peakEl = el;
      }
    } else if (open) {
      // Refine the edges against the bracket we already have; a 30 second grid
      // would otherwise report horizon times a whole minute wrong.
      open.aos = bisect(o, observer, open.aos - stepMs, open.aos, minElevation, scratch, true);
      open.los = bisect(o, observer, t - stepMs, t, minElevation, scratch, false);
      passes.push(open);
      open = null;
      if (passes.length >= maxPasses) break;
    }

    previous = t;
  }

  if (open) {
    open.aos = bisect(o, observer, open.aos - stepMs, open.aos, minElevation, scratch, true);
    open.los = end;
    passes.push(open);
  }

  return passes.map((p) => ({
    aos: Math.round(p.aos),
    los: Math.round(p.los ?? end),
    peak: Math.round(p.peak),
    peakEl: Math.round(p.peakEl * 10) / 10,
    durationSec: Math.round(((p.los ?? end) - p.aos) / 1000),
  }));
}

/** Find where elevation crosses the threshold inside [a, b]. */
function bisect(o, observer, a, b, threshold, scratch, rising) {
  let lo = a;
  let hi = b;
  for (let i = 0; i < 14; i++) {
    const mid = (lo + hi) / 2;
    const above = elevationAt(o, observer, mid, scratch) >= threshold;
    if (above === rising) hi = mid;
    else lo = mid;
  }
  return Math.round((lo + hi) / 2);
}

/**
 * Sub-satellite track between two instants, as lon/lat pairs.
 * Used for the ground track: the past drawn dim, the predicted arc drawn bright.
 */
export function groundTrack(o, fromMs, toMs, stepSec = 60) {
  const stepMs = Math.max(5, stepSec) * 1000;
  const scratch = { x: 0, y: 0, z: 0 };
  const points = [];

  for (let t = fromMs; t <= toMs; t += stepMs) {
    points.push(toGeodetic(propagate(o, t, scratch)));
  }

  return points;
}

/* -- classification ------------------------------------------------------- */

/** Coarse orbit class, used only to colour the globe. */
export function orbitClass(o) {
  if (o.ecc > 0.25) return 'heo';
  if (o.revPerDay >= 11) return 'leo';
  if (o.revPerDay >= 1.9) return 'meo';
  if (o.revPerDay >= 0.9) return 'geo';
  return 'heo';
}

export const ORBIT_MU = MU;
export const ORBIT_RE = RE;
