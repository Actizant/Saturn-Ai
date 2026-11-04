/**
 * Saturn's View -- the city register.
 *
 * Real OpenStreetMap footprints, extruded into volumes and stroked in the
 * same amber as everything else. No fill, no texture, no shading: the
 * buildings are drawn, not lit, and what makes a volume read is its outline --
 * the walls facing you and the corner where that stops being true.
 *
 * This layer exists because a globe is a picture of a planet and stops being
 * one at about forty kilometres. Below that the interesting structure is the
 * thing people actually built, and no amount of coastline detail substitutes
 * for it.
 *
 * Three decisions worth keeping:
 *
 *   Height is stated or it is not. OSM carries `height` on a small minority of
 *   buildings and `building:levels` on more. Where neither exists the type is
 *   the only evidence left, and a median for that type is a guess. The payload
 *   counts all three separately and the readout says the percentage, because a
 *   skyline is exactly the kind of thing an operator will trust without asking.
 *
 *   A footprint is only worth drawing when you are among it. Below a kilometre
 *   and a half the wireframe fills the screen and reads as a street; from ten
 *   kilometres the same geometry is a smear of hairlines on a ball. So the
 *   register has a ceiling on the camera's altitude, and the number is not a
 *   performance guard -- it is what the only keyless source fast enough to
 *   feel live will actually serve. Beyond about two and a half kilometres of
 *   ground the planet's map endpoint refuses outright, and the alternative
 *   queues every request at half a minute regardless of how small it is.
 *
 *   The window follows the camera, and it is the registry that does the
 *   fetching. This layer never owns a timer: it watches the camera settle and
 *   calls `retrigger`, so cadence, backoff and failure reporting stay in one
 *   place for every layer in the engine.
 */

const GOLD = '#E8B84B';

/**
 * The brightest ink: the same amber, taken all the way up.
 *
 * A solid is not a plan seen from a better angle -- it is the thing the frame
 * is about -- and on a black ground the only way to rank two amber lines is
 * luminance. Warm white rather than white, so it still belongs to the family:
 * one wavelength, four intensities.
 */
const SOLID = '#FFF4DE';

/**
 * The graticule's own cold grey, borrowed for the survey grid.
 *
 * Two inks and no third wavelength: grey is structure -- graph paper, a
 * reference frame, the thing that is not data -- and amber is what was
 * measured. Which means the eye can tell, without a legend, that the grid is
 * not a street network it should be reading.
 */
const SURVEY = '#9FB3C8';

/** Above this the register draws nothing and says why, rather than guessing. */
const ENTER_HEIGHT = 1_500;

/** The window the camera can turn into one request, in degrees. */
const MIN_SPAN = 0.004;
const MAX_SPAN = 0.025;
/** The same ceiling in metres, which is the unit a frustum is measured in. */
const MAX_REACH = 2_600;

/**
 * How many footprints survive the cull. The provider caps at six thousand;
 * this is the layer's own budget for what it will hold in the scene, and the
 * readout says how much of the answer was drawn.
 */
const MAX_BUILDINGS = 2400;

/** A camera that has stopped for this long is asking for the ground it sees. */
const SETTLE_MS = 650;
/** How often a settled camera is re-examined, so ten hertz is not spent on it. */
const CHECK_MS = 400;
/** Longer than this and the operator deserves an answer rather than a wait. */
const FETCH_TIMEOUT_MS = 25_000;

/** Whether the camera is low enough for footprints to mean anything at all. */
function insideRegister(at) {
  return Boolean(at) && Number.isFinite(at.height) && at.height <= ENTER_HEIGHT;
}

export const cityLayer = {
  id: 'city',
  title: 'City',
  group: 'earth',
  source: 'OpenStreetMap',
  // Footprints do not move, so the cadence is a repair interval rather than a
  // poll. Movement comes from the camera, through `retrigger`.
  cadence: 6 * 60 * 1000,

  create({ CESIUM, scene, camera, clock, requestRefresh, retrigger }) {
    const gold = CESIUM.Color.fromCssColorString(GOLD);

    /**
     * One collection, one width, four inks.
     *
     * This is not a stylistic preference, it is a measurement. A polyline
     * collection is batched per material and width, and the price of the batch
     * scales with how many of those there are rather than with how many lines:
     * this drawing split across six collections cost a hundred and ninety
     * milliseconds a frame, the same lines in one collection cost ninety, and
     * a single batched primitive of five thousand lines cost ten. The fade
     * that six collections bought was not worth eighteen frames.
     *
     * Four materials inside one collection is the compromise the measurement
     * allows: the geometry stays one buffer set, and the batching is by
     * material within it. What the four buy is hierarchy -- survey quieter
     * than plan, plan quieter than solid, and the brightest ink spent on the
     * few buildings whose walls can actually be seen, so the drawing gets more
     * graphic as the camera comes down and flatter as it goes up, which is
     * what looking at a city does. The step from amber to warm white is not a
     * second colour, it is the same line with the light turned up: on black,
     * that is the only ranking the eye reads at a glance.
     */
    const lines = scene.primitives.add(new CESIUM.PolylineCollection());
    const ink = {
      survey: CESIUM.Material.fromType('Color', {
        color: CESIUM.Color.fromCssColorString(SURVEY).withAlpha(0.13),
      }),
      plan: CESIUM.Material.fromType('Color', { color: gold.withAlpha(0.3) }),
      body: CESIUM.Material.fromType('Color', {
        color: CESIUM.Color.fromCssColorString(SOLID).withAlpha(0.92),
      }),
      // Straight down there are no bodies to carry the drawing -- the camera
      // looks along every wall in the city -- so the ink that is left has to
      // carry it instead. One more material, never used at the same time as
      // the others, so it costs a batch only on the frames it is drawn.
      flat: CESIUM.Material.fromType('Color', { color: gold.withAlpha(0.7) }),
      // The edge of what the source answered for: the same cold grey as the
      // grid, brighter, because it is the end of the paper rather than the
      // pattern on it.
      edge: CESIUM.Material.fromType('Color', {
        color: CESIUM.Color.fromCssColorString(SURVEY).withAlpha(0.34),
      }),
    };

    /**
     * The tags.
     *
     * A label on every roof is a spreadsheet. Four or five, on the loudest
     * walls with room between them, is a profiler: it gives the eye somewhere
     * to start and it says the one number the source actually stated.
     */
    const tags = scene.primitives.add(new CESIUM.LabelCollection());
    const MAX_TAGS = 5;

    /**
     * How many buildings are allowed a body.
     *
     * A body is one polyline, so this is a count of objects rather than of
     * walls. It is spent on the buildings whose walls are largest on screen,
     * which is the only ordering that survives being tilted, zoomed or flown
     * along: whatever is most visible gets the budget, by arithmetic rather
     * than by a distance someone guessed.
     */
    const MAX_BODIES = 360;

    /** A wall shorter than this on screen is a dot, not a body. */
    const MIN_EDGE_PX = 2;

    let buildings = [];
    let loaded = { truncated: false, stated: {}, span: 0, count: 0, box: null };
    /** Rows and columns of the survey grid for the window that is loaded. */
    let survey = { rows: [], step: 0 };
    let painted = { polylines: 0, ms: 0 };
    let drawn = 0;
    let bodied = 0;
    let requestedKey = null;
    let retryAfter = 0;

    let settled = 0;
    let lastCheck = 0;
    let lastSeen = { lat: NaN, lon: NaN, height: NaN };
    let unsubscribe = null;

    // ------------------------------------------------------------- window //

    /**
     * Snap a request to the provider's grid.
     *
     * Both the camera and the fetch go through this, so the key computed while
     * watching the camera is the same key the fetch computes a moment later.
     * Two rounding rules would make the layer refetch itself forever.
     */
    function snap(lat, lon, span) {
      const grid = MIN_SPAN;
      const qLat = Math.round(lat / grid) * grid;
      const qLon = Math.round(lon / grid) * grid;
      const qSpan = Math.round(span / grid) * grid;
      return {
        lat: qLat,
        lon: qLon,
        span: qSpan,
        key: `${qLat.toFixed(3)}:${qLon.toFixed(3)}:${qSpan.toFixed(3)}`,
      };
    }

    /**
     * How far along its own heading the camera can see the ground.
     *
     * `computeViewRectangle` answers this well for a camera pointed straight
     * down and badly for one pointed forward: a tilted frustum runs to the
     * horizon, so its bounding box is centred kilometres away and a request
     * built from it lands on ground that is not in the frame. Pitch, heading
     * and altitude are the three numbers that actually turn a camera into a
     * strip of ground, so the window is derived from those instead.
     *
     * Returns metres: where the middle of the frame meets the ground, and how
     * much ground the frame holds.
     */
    function reachFor(at) {
      const fovy = scene.camera?.frustum?.fovy || Math.PI / 3;
      const aspect =
        scene.camera?.frustum?.aspectRatio ||
        (scene.canvas?.clientWidth || 16) / (scene.canvas?.clientHeight || 9);
      const half = (fovy * 90) / Math.PI; // half the vertical field of view
      const height = Math.max(1, at.height);
      const pitch = Number.isFinite(at.pitch) ? at.pitch : -90;

      // Both edges clamped, so a ray aimed past the horizon or past straight
      // down lands on the limit rather than at infinity.
      const floorEdge = Math.max(-89.5, pitch - half);
      const roofEdge = Math.min(-0.5, pitch + half);

      const near = height / Math.tan((-floorEdge * Math.PI) / 180);
      const far = Math.min(height / Math.tan((-roofEdge * Math.PI) / 180), MAX_REACH);
      const centre = Math.min(height / Math.tan((-Math.min(-0.5, pitch) * Math.PI) / 180), MAX_REACH);

      // Looking straight down the frame is wider than it is tall, and the
      // width is the side the strip calculation cannot see: half the width of
      // the frame at the camera's own distance, doubled.
      const across = height * Math.tan(Math.atan(Math.tan(fovy / 2) * aspect));

      // A square request cannot hold a trapezium that widens all the way to
      // the horizon, so it is sized to keep the near ground and as much of the
      // far as the source will serve in one answer.
      return { centre, span: 2 * Math.max(far - centre, centre - near, across) };
    }

    /**
     * The point on the ground the middle of the frame is over.
     *
     * Straight down this is the ground under the camera; tilted it is a
     * kilometre ahead. Both the fetch window and the body budget are measured
     * from here, because measuring a tilted frame from under the camera spends
     * the whole budget behind the bottom edge.
     */
    function lookAt(at) {
      const { centre } = reachFor(at);
      const radians = ((at.heading || 0) * Math.PI) / 180;
      const cos = Math.max(0.15, Math.cos((at.lat * Math.PI) / 180));
      return {
        lat: at.lat + (centre * Math.cos(radians)) / 111_320,
        lon: at.lon + (centre * Math.sin(radians)) / (111_320 * cos),
      };
    }

    function windowFor(at) {
      if (!insideRegister(at)) return null;

      const { span } = reachFor(at);
      const { lat, lon } = lookAt(at);
      // Longitude degrees shrink with latitude, but the span is a span in
      // metres, and the provider square-roots its own box.
      const degrees = Math.min(MAX_SPAN, Math.max(MIN_SPAN, span / 111_320));

      return snap(lat, lon, degrees);
    }

    // -------------------------------------------------------------- paint //

    function parse(rows, origins) {
      if (!Array.isArray(rows)) return [];

      const out = [];
      for (let index = 0; index < rows.length; index += 1) {
        const row = rows[index];
        // [height, lon, lat, lon, lat, ...] -- at least a triangle.
        if (!Array.isArray(row) || row.length < 7) continue;

        const height = Number(row[0]);
        if (!Number.isFinite(height) || height <= 0) continue;

        const ring = row.slice(1);
        if (ring.length % 2 !== 0) continue;

        const count = ring.length >> 1;
        let lat = 0;
        let lon = 0;
        for (let i = 0; i < count; i += 1) {
          lon += ring[i * 2];
          lat += ring[i * 2 + 1];
        }

        // Whether the number came from a tag or from the median for the
        // building's type. A missing code is read as "not stated", because
        // an unlabelled number is exactly the case that has to be marked.
        const stated = Array.isArray(origins) && Number(origins[index]) > 0;

        // The centroid is the cull key: a building is kept or dropped by how
        // far its body is from what the camera is looking at. `deck` is the
        // roof ring converted to Cartesian once and kept: a footprint does not
        // move, so re-converting the whole window on every pan was ten
        // thousand points of garbage for geometry that had not changed.
        out.push({ height, lat: lat / count, lon: lon / count, ring, deck: null, stated });
      }
      return out;
    }

    /**
     * Drop every polyline the register has drawn, and leave the collections in
     * the scene. Emptying a collection is cheap and keeps the primitives alive,
     * so the next paint does not pay for re-adding them.
     */
    function clear() {
      drawn = 0;
      lines.removeAll();
      tags.removeAll();
    }

    /** A footprint at roof height: the plan of a building whose walls we cannot see. */
    function roofRing(ring, count, height) {
      const path = new Float64Array((count + 1) * 3);
      for (let i = 0; i < count; i += 1) {
        path[i * 3] = ring[i * 2];
        path[i * 3 + 1] = ring[i * 2 + 1];
        path[i * 3 + 2] = height;
      }
      path[count * 3] = path[0];
      path[count * 3 + 1] = path[1];
      path[count * 3 + 2] = path[2];
      return path;
    }

    /**
     * The survey grid: the only thing in this drawing that is not measured.
     *
     * It is not a street network and must not look like one. Nothing in the
     * footprint source says where the roads are, so a plausible road map drawn
     * here would be the one lie in an otherwise sourced picture -- and a lie is
     * worse than a grid. What it is, is graph paper: round numbers of metres,
     * at the coarsest spacing that still puts a dozen-odd lines across the
     * frame, sitting under the city so the city has something to be measured
     * against. The count is the whole of the taste in this decision and it is
     * deliberately low: a grid dense enough to read as texture stops being a
     * scale and starts competing with the buildings for the eye, which a
     * top-down view of two and a half kilometres of city did on the first try.
     * The step is reported in the snapshot and named in the readout, so
     * whatever is on screen is stating a fact rather than choosing a caption.
     */
    const GRID_STEPS = [10, 25, 50, 100, 250, 500, 1_000];

    /** Lines per side. Sixteen buys a scale; forty is a screenful of texture. */
    const GRID_LINES = 16;

    /** Drawn this far above the ellipsoid, so it is a grid and not a z-fight. */
    const GRID_LIFT = 1;

    function surveyFor(box) {
      const metres = box.span * 111_320;
      const step =
        GRID_STEPS.find((candidate) => metres / candidate <= GRID_LINES) ??
        GRID_STEPS[GRID_STEPS.length - 1];

      const half = box.span / 2;
      const cos = Math.max(0.15, Math.cos((box.lat * Math.PI) / 180));
      // Degrees per step, and square cells: a degree of longitude is shorter
      // than a degree of latitude everywhere but the equator, so the columns
      // are spaced wider to make the cells the same size in metres.
      const dLat = step / 111_320;
      const dLon = dLat / cos;

      const west = box.lon - half;
      const east = box.lon + half;
      const south = box.lat - half;
      const north = box.lat + half;

      const out = [];
      // Anchored to multiples of the step, so the grid is the same grid from
      // one window to the next and slides rather than shimmers.
      for (let lat = Math.ceil(south / dLat) * dLat; lat <= north; lat += dLat) {
        out.push([west, lat, GRID_LIFT, east, lat, GRID_LIFT]);
      }
      for (let lon = Math.ceil(west / dLon) * dLon; lon <= east; lon += dLon) {
        out.push([lon, south, GRID_LIFT, lon, north, GRID_LIFT]);
      }
      return { rows: out, step };
    }

    function gridStep() {
      return survey.step || 0;
    }

    /**
     * A footprint with a volume standing on it, drawn the way a technical
     * illustration draws a solid: only the edges you can see from here.
     *
     * A wall is visible when its outward direction agrees with the direction to
     * the camera. Those walls give the footprint you can see, the two corners
     * where that stops being true give the outer verticals, and the roof ring
     * closes the top. The far footprint is not drawn, because on a solid it is
     * not an edge.
     *
     * The creases are the part that took a screenshot to learn. An outline
     * alone reads as a tray: a box seen from above shows its roof, the two
     * outer verticals and the near base, and with nothing between them the eye
     * sees a rim rather than a corner. So every vertex that actually turns --
     * more than about fifteen degrees, because a curved facade carries points
     * that are not corners -- gets a vertical from its base to its roof, which
     * is the edge where two visible faces meet and the line that makes the
     * thing solid.
     *
     * One polyline still: the roof is traced all the way round, the path drops
     * down the near corner, runs back along the visible footprint, climbs and
     * drops at each crease, and finishes up the far vertical. Retracing is
     * free -- a line drawn over itself looks exactly like the line underneath
     * -- so the whole solid stays in one object.
     */
    function silhouette(building, ring, count, height, eye) {
      const dx = (eye.lat - building.lat) * 111_320;
      const dy = (eye.lon - building.lon) * 111_320 * eye.cos;

      const faces = (i) => {
        const j = (i + 1) % count;
        const mx = ((ring[i * 2 + 1] + ring[j * 2 + 1]) / 2 - building.lat) * 111_320;
        const my = ((ring[i * 2] + ring[j * 2]) / 2 - building.lon) * 111_320 * eye.cos;
        return mx * dx + my * dy > 0;
      };

      // On a convex block the visible walls are one unbroken run around the
      // footprint. A concave one can have several, and the longest is drawn:
      // a gap in an outline is better than a line across the roof where two
      // runs were joined.
      let bestStart = 0;
      let bestLength = 0;
      let runStart = 0;
      let runLength = 0;
      for (let k = 0; k < 2 * count; k += 1) {
        if (faces(k % count)) {
          if (runLength === 0) runStart = k;
          runLength += 1;
          if (runLength > bestLength) {
            bestLength = Math.min(runLength, count);
            bestStart = runStart;
          }
        } else {
          runLength = 0;
        }
      }

      if (bestLength === 0) return roofRing(ring, count, height);

      const start = bestStart % count;

      /**
       * Whether this vertex is a corner rather than a point on a curve.
       *
       * Fifteen degrees: below that the "crease" is two walls nearly in the
       * same plane, and a vertical there is a line the eye reads as noise on a
       * facade that was surveyed as a smooth run of nodes.
       */
      const CREASE_COS = 0.966;

      const turns = (index) => {
        const prev = (index + count - 1) % count;
        const next = (index + 1) % count;
        const ax = (ring[index * 2] - ring[prev * 2]) * eye.cos;
        const ay = ring[index * 2 + 1] - ring[prev * 2 + 1];
        const bx = (ring[next * 2] - ring[index * 2]) * eye.cos;
        const by = ring[next * 2 + 1] - ring[index * 2 + 1];
        const lengths = (Math.hypot(ax, ay) || 1) * (Math.hypot(bx, by) || 1);
        return (ax * bx + ay * by) / lengths < CREASE_COS;
      };

      // The roof, then the near corner, then the base run with a vertical at
      // every crease in it, and up the far corner: the base never has more
      // than one extra point per wall, and each crease costs three.
      const path = new Float64Array((count + 4 * bestLength + 3) * 3);
      let w = 0;
      const put = (index, z) => {
        path[w] = ring[index * 2];
        path[w + 1] = ring[index * 2 + 1];
        path[w + 2] = z;
        w += 3;
      };

      put(start, height); // the near corner, on the roof
      for (let k = 1; k <= count; k += 1) {
        put((start + k) % count, height); // the roof, all the way round and back
      }
      put(start, 0); // down the near corner
      for (let k = 1; k <= bestLength; k += 1) {
        const index = (start + k) % count;
        put(index, 0); // the footprint the visible walls stand on
        if (k < bestLength && turns(index)) {
          put(index, height); // up the crease between two visible walls
          put(index, 0); // and straight back down it
        }
      }
      put((start + bestLength) % count, height); // and up the far corner

      // The buffer is an upper bound -- how many creases a run has depends on
      // the footprint -- and the renderer reads every vertex it is handed. An
      // unwritten tail would be a point on the ellipsoid at zero degrees, and
      // a line from Mumbai to the Gulf of Guinea is a long line.
      return path.subarray(0, w);
    }

    /**
     * Ground point to canvas point.
     *
     * The name changed in Cesium 1.121 from `wgs84ToWindowCoordinates` to
     * `worldToWindowCoordinates`, and this tree has been built against both at
     * different times. Resolved once, and allowed to be missing: without it the
     * tags lose their spacing test and still draw, which is a worse drawing
     * rather than a broken layer.
     */
    const project =
      CESIUM.SceneTransforms?.worldToWindowCoordinates ||
      CESIUM.SceneTransforms?.wgs84ToWindowCoordinates ||
      null;

    function paint() {
      clear();
      const started = performance.now();

      if (buildings.length === 0) {
        requestRefresh?.();
        return;
      }

      // The point the camera is looking at, not the point it is sitting over.
      // On a tilted camera those are a kilometre apart, and measuring the
      // body budget from the wrong one spends all of it on ground that is
      // behind the bottom edge of the frame.
      const at = camera.view();
      if (!at) {
        requestRefresh?.();
        return;
      }
      const focus = lookAt(at);
      const cos = Math.max(0.15, Math.cos((focus.lat * Math.PI) / 180));

      // Whether a wall can be seen at all.
      //
      // Straight down, the camera looks along every vertical in the city and
      // they project to points; from a kilometre away a five-metre wall is
      // under two pixels. Both cases used to be drawn, which is thousands of
      // polylines that could not appear on any screen. The test is the same
      // arithmetic the projection does, so a body is drawn exactly when its
      // walls are long enough to be worth looking at.
      const tallness = Math.cos(((at.pitch ?? -90) * Math.PI) / 180);
      const fovy = scene.camera?.frustum?.fovy || 1;
      const pixelsPerMetre = (scene.canvas?.clientHeight || 900) / fovy;
      const altitude = at.height || 0;

      // Nearest first, so a cull takes the far streets and leaves the ones
      // under the camera. A city that stops being drawn at its edge is a
      // horizon; a city with a hole in the middle is a bug.
      const ranked = buildings
        .map((building) => {
          const dLat = (building.lat - focus.lat) * 111_320;
          const dLon = (building.lon - focus.lon) * 111_320 * cos;
          return { building, distance: Math.hypot(dLat, dLon) };
        })
        .sort((a, b) => a.distance - b.distance);

      const chosen = ranked.length > MAX_BUILDINGS ? ranked.slice(0, MAX_BUILDINGS) : ranked;

      // Then, out of what survived the cull, spend the bodies on the walls
      // that are tallest on screen. Straight down not one of them qualifies,
      // because the camera is looking along every wall in the city.
      const loud = chosen
        .map((entry) => ({
          building: entry.building,
          px:
            (entry.building.height * tallness * pixelsPerMetre) /
            (Math.hypot(altitude, entry.distance) || 1),
        }))
        .filter((entry) => entry.px >= MIN_EDGE_PX)
        .sort((a, b) => b.px - a.px)
        .slice(0, MAX_BODIES);

      const bodiedSet = new Set(loud.map((entry) => entry.building));
      // Where the viewer stands, in the same metres the footprint test uses.
      const eye = { lat: at.lat, lon: at.lon, cos };

      // The edge of the answer, before anything stands on it.
      //
      // Without it the city stops where the source stopped and the eye reads
      // the last street as a cliff into nothing. With it the drawing says
      // where it ends: a square of surveyed ground on black, which is both a
      // true statement and the frame the rest of the picture needs. It is the
      // only line here that is a claim about knowledge rather than about the
      // city, so it is grey like the grid and nothing else uses that ink.
      const { box } = loaded;
      if (box) {
        const half = box.span / 2;
        lines.add({
          positions: CESIUM.Cartesian3.fromDegreesArrayHeights([
            box.lon - half, box.lat - half, GRID_LIFT,
            box.lon + half, box.lat - half, GRID_LIFT,
            box.lon + half, box.lat + half, GRID_LIFT,
            box.lon - half, box.lat + half, GRID_LIFT,
            box.lon - half, box.lat - half, GRID_LIFT,
          ]),
          width: 1,
          material: ink.edge,
        });
      }

      // Graph paper under the drawing, first, so nothing is drawn over it.
      const tGrid = performance.now();
      for (const row of survey.rows || []) {
        lines.add({
          positions: CESIUM.Cartesian3.fromDegreesArrayHeights(row),
          width: 1,
          material: ink.survey,
        });
      }
      const tRings = performance.now();

      // Split by ink before drawing, because a polyline collection groups its
      // draws by material: every time the ink changes partway down the list,
      // another draw command is cut. Interleaving plans and solids -- which is
      // what walking the buildings in distance order does -- cuts one command
      // per alternation, and at a few hundred solids that was the whole
      // frame's budget. Two arrays and three contiguous runs instead.
      const plans = [];
      const solids = [];
      for (const { building } of chosen) {
        (bodiedSet.has(building) ? solids : plans).push(building);
      }

      // Plans first, solids last, so the volumes are drawn over the plan work
      // they were measured out of.
      const planInk = solids.length ? ink.plan : ink.flat;
      for (const [batch, material] of [
        [plans, planInk],
        [solids, ink.body],
      ]) {
        for (const building of batch) {
          const ring = building.ring;
          const count = ring.length >> 1;
          let positions;

          if (material === ink.body) {
            // A body changes with the direction it is seen from, so it is
            // rebuilt; the deck under it does not, so it is kept.
            positions = CESIUM.Cartesian3.fromDegreesArrayHeights(
              silhouette(building, ring, count, building.height, eye)
            );
          } else {
            building.deck ??= CESIUM.Cartesian3.fromDegreesArrayHeights(
              roofRing(ring, count, building.height)
            );
            positions = building.deck;
          }

          lines.add({ positions, width: 1, material });
        }
      }

      // The tags come after the draw so they sit on top of it. The tallest
      // buildings, chosen by where they land on the canvas rather than by
      // where they sit on the ground: a rooftop half a kilometre behind the
      // camera projects perfectly outside the frame, and a label for it is a
      // label nobody will ever see.
      const notable = (loud.length ? loud.map((entry) => entry.building) : chosen.map((entry) => entry.building))
        .slice()
        .sort((a, b) => b.height - a.height);

      const width = scene.canvas?.clientWidth || 1600;
      const height = scene.canvas?.clientHeight || 900;
      const tTags = performance.now();
      const tagged = [];
      for (let i = 0; i < notable.length && tagged.length < MAX_TAGS; i += 1) {
        const candidate = notable[i];
        const screen = project
          ? project.call(
              CESIUM.SceneTransforms,
              scene,
              CESIUM.Cartesian3.fromDegrees(candidate.lon, candidate.lat, candidate.height)
            )
          : null;
        if (!screen) continue;
        const inside =
          screen.x > 32 && screen.x < width - 32 && screen.y > 40 && screen.y < height - 32;
        if (!inside) continue;
        const clear = tagged.every((other) => Math.hypot(other.x - screen.x, other.y - screen.y) > 90);
        if (clear) tagged.push({ building: candidate, x: screen.x, y: screen.y });
      }

      for (const { building } of tagged) {
        // A tilde on a guess and a plain number on a measurement, because on
        // this dataset most of a skyline is an inference from the building's
        // type and the drawing would otherwise be stating it as fact. The ink
        // follows the same rule: grey is what nobody told us.
        tags.add({
          position: CESIUM.Cartesian3.fromDegrees(building.lon, building.lat, building.height + 4),
          text: `${building.stated ? '' : '~'}${Math.round(building.height)} m`,
          font: '11px "Cascadia Mono", Consolas, monospace',
          style: CESIUM.LabelStyle.FILL,
          fillColor: building.stated
            ? CESIUM.Color.fromCssColorString(SOLID).withAlpha(0.95)
            : CESIUM.Color.fromCssColorString(SURVEY).withAlpha(0.85),
          outlineColor: CESIUM.Color.fromCssColorString('#04070C').withAlpha(0.9),
          outlineWidth: 2,
          showBackground: true,
          backgroundColor: CESIUM.Color.fromCssColorString('#04070C').withAlpha(0.82),
          backgroundPadding: new CESIUM.Cartesian2(7, 4),
          pixelOffset: new CESIUM.Cartesian2(0, -15),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        });
      }

      drawn = chosen.length;
      bodied = loud.length;
      // What the last paint cost, so a slow frame can be blamed on the drawing
      // or cleared of it without a profiler attached.
      painted = {
        polylines: lines.length,
        ms: Math.round((performance.now() - started) * 10) / 10,
        grid: Math.round((tRings - tGrid) * 10) / 10,
        rings: Math.round((tTags - tRings) * 10) / 10,
        tags: Math.round((performance.now() - tTags) * 10) / 10,
      };
      requestRefresh?.();
    }

    // ------------------------------------------------------------ refresh //

    async function load(box) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
      try {
        const url =
          `/api/buildings?lat=${box.lat.toFixed(4)}` +
          `&lon=${box.lon.toFixed(4)}&span=${box.span.toFixed(4)}`;
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`footprints replied ${response.status}`);
        return await response.json();
      } finally {
        clearTimeout(timer);
      }
    }

    async function refresh() {
      const at = camera.view();
      const wanted = windowFor(at);

      if (!wanted) {
        // Too high to be a city. The geometry is dropped from the scene but
        // kept in memory: coming back down should be instant, not a refetch.
        requestedKey = null;
        if (buildings.length) clear();
        return { count: 0, source: 'OpenStreetMap' };
      }

      let box = wanted;
      let payload;
      try {
        payload = await load(box);

        // The server caps what it will return. A city that came back short
        // asks again for less ground, rather than drawing one rectangle of
        // city and leaving a cliff through the middle of the rest.
        for (let attempt = 0; payload.truncated && attempt < 2 && box.span > MIN_SPAN * 1.6; attempt += 1) {
          box = snap(box.lat, box.lon, box.span * 0.6);
          payload = await load(box);
        }
      } catch (err) {
        requestedKey = null;
        retryAfter = Date.now() + 30_000;
        throw err;
      }

      buildings = parse(payload.buildings, payload.origins);
      // The grid belongs to the answer, not to the camera: it is drawn for the
      // ground that actually came back, so it stops exactly where the city
      // does instead of running out to a window that was retried smaller.
      survey = surveyFor(box);
      loaded = {
        truncated: Boolean(payload.truncated),
        stated: payload.stated || {},
        span: box.span,
        count: payload.count ?? buildings.length,
        box,
      };

      // The key the camera asked with, not the one the answer came from. When
      // a truncated answer is retried on less ground the two differ, and
      // storing the smaller one would make the next tick see a new key and
      // refetch the same ground forever.
      requestedKey = wanted.key;
      retryAfter = 0;
      paint();

      return { count: buildings.length, source: payload.source || 'OpenStreetMap' };
    }

    // --------------------------------------------------------------- tick //

    function tick({ now, dt }) {
      const at = camera.view();
      if (!at || !Number.isFinite(at.height)) return;

      const moved =
        Math.abs(at.lat - lastSeen.lat) > 1e-7 ||
        Math.abs(at.lon - lastSeen.lon) > 1e-7 ||
        Math.abs(at.height - lastSeen.height) > 1;

      if (moved) {
        lastSeen = at;
        settled = 0;
      } else {
        settled += dt || 0;
      }

      if (settled < SETTLE_MS) return;
      if (now - lastCheck < CHECK_MS) return;
      lastCheck = now;

      const window = windowFor(at);
      const key = window ? window.key : null;
      if (key === requestedKey) return;
      if (Date.now() < retryAfter) return;

      requestedKey = key;
      retrigger?.();
    }

    // ------------------------------------------------------------ readout //

    function readout() {
      const at = camera.view();

      if (buildings.length === 0) {
        if (!insideRegister(at)) return 'fly below 1.5 km';
        return '';
      }

      const stated = loaded.stated || {};
      const fromTag = (stated.height || 0) + (stated.levels || 0);
      const share = Math.round((fromTag / buildings.length) * 100);

      const head =
        drawn < buildings.length
          ? `${drawn.toLocaleString()} of ${buildings.length.toLocaleString()}`
          : buildings.length.toLocaleString();

      // The grid step is the picture's scale, and a map without one is a
      // drawing of unknown size. It is stated rather than drawn as a bar
      // because the rail is text and the number is already known exactly.
      const scale = survey.step ? ` \u00b7 ${survey.step} m grid` : '';

      return `${head} \u00b7 ${share}% stated${scale}`;
    }

    return {
      start() {
        unsubscribe = clock.subscribe(tick);
      },

      refresh,

      readout,

      snapshot() {
        return {
          loaded: buildings.length,
          drawn,
          bodies: bodied,
          tags: tags.length,
          grid: gridStep(),
          polylines: painted.polylines,
          paintMs: painted.ms,
          paintPhases: painted,
          visible: drawn > 0,
          truncated: loaded.truncated,
          stated: loaded.stated,
          span: loaded.span,
        };
      },

      dispose() {
        if (unsubscribe) {
          unsubscribe();
          unsubscribe = null;
        }
        buildings = [];
        survey = { rows: [], step: 0 };
        lines.removeAll();
        tags.removeAll();
        scene.primitives.remove(lines);
        scene.primitives.remove(tags);
      },
    };
  },
};
