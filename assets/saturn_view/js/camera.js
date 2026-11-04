/**
 * Saturn's View -- camera.
 *
 * One place that knows how to look at the planet. Layers ask it what is on
 * screen; the assistant asks it to go somewhere. Nothing else touches
 * viewer.camera directly, so every movement is announcable and reversible.
 *
 * Every flight is issued through `flyTo`, which means the engine can always
 * report where it is going and cancel it cleanly when a new request arrives.
 */

const HOME = Object.freeze({ lat: 21.1, lon: 78.9, height: 9_000_000 });

export function createCamera({ CESIUM, viewer, bus, CHANNELS, home = HOME }) {
  let lastEmit = 0;
  let pending = null;

  function view() {
    const carto = viewer.camera.positionCartographic;
    return {
      lat: CESIUM.Math.toDegrees(carto.latitude),
      lon: CESIUM.Math.toDegrees(carto.longitude),
      height: carto.height,
      // How flat the view is, in the same degrees as everything else here:
      // -90 looks straight down, 0 looks at the horizon. A layer that draws
      // anything with height needs this, because a vertical seen from directly
      // above projects to a point and drawing it is drawing nothing.
      pitch: CESIUM.Math.toDegrees(viewer.camera.pitch),
      // Where the camera is pointing, measured clockwise from north. Together
      // with pitch this turns an altitude into the strip of ground actually
      // in frame, which is what a layer that fetches a window needs.
      heading: CESIUM.Math.toDegrees(viewer.camera.heading),
    };
  }

  /** The lon/lat box currently on screen, or null when the camera sees space. */
  function viewport() {
    try {
      const rect = viewer.camera.computeViewRectangle(viewer.scene.globe.ellipsoid);
      if (!rect) return null;
      const deg = (rad) => CESIUM.Math.toDegrees(rad);
      const west = deg(rect.west);
      const south = deg(rect.south);
      const east = deg(rect.east);
      const north = deg(rect.north);
      const span = Math.max(east - west, north - south);
      // A bounding box wider than a hemisphere is centred on the intersection
      // of the equator and the prime meridian -- the Gulf of Guinea -- no
      // matter where the camera actually is. Answering "what is in view" with
      // a patch of empty ocean is worse than admitting the view is global, so
      // callers get null and fall back to the sub-camera point.
      const GLOBE_SPAN_DEG = 140;
      if (!Number.isFinite(span) || span <= 0 || span >= GLOBE_SPAN_DEG) return null;
      return { west, south, east, north, span };
    } catch {
      return null;
    }
  }

  function cancel() {
    if (pending) {
      viewer.camera.cancelFlight();
      pending = null;
    }
  }

  function flyTo({ lat, lon, height = 2_000_000, duration = 2.4, heading = 0, pitch = -90 }) {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      throw new Error('flyTo needs finite lat and lon');
    }
    cancel();
    pending = { lat, lon, height };
    viewer.camera.flyTo({
      destination: CESIUM.Cartesian3.fromDegrees(lon, lat, height),
      orientation: { heading: CESIUM.Math.toRadians(heading), pitch: CESIUM.Math.toRadians(pitch), roll: 0 },
      duration,
      complete: () => {
        pending = null;
      },
      cancel: () => {
        pending = null;
      },
    });
    return { lat, lon, height };
  }

  /** Frame a set of positions with sensible padding. */
  function frame(points, { duration = 1.6 } = {}) {
    const list = (points || []).filter(
      (p) => Number.isFinite(p?.lat) && Number.isFinite(p?.lon)
    );
    if (list.length === 0) return false;
    if (list.length === 1) return flyTo({ ...list[0], height: 900_000, duration });

    const lats = list.map((p) => p.lat);
    const lons = list.map((p) => p.lon);
    const rect = CESIUM.Rectangle.fromDegrees(
      Math.min(...lons),
      Math.min(...lats),
      Math.max(...lons),
      Math.max(...lats)
    );
    cancel();
    viewer.camera.flyTo({ destination: rect, duration });
    return true;
  }

  function homeView({ duration = 2.6 } = {}) {
    return flyTo({ ...home, duration });
  }

  /** Announce movement, throttled -- layers must not be re-polled per frame. */
  function onMove(callback) {
    return viewer.camera.changed.addEventListener(() => {
      const now = Date.now();
      if (now - lastEmit < 300) return;
      lastEmit = now;
      const payload = { ...view(), viewport: viewport() };
      bus.emit(CHANNELS.CAMERA_MOVED, payload);
      callback?.(payload);
    });
  }

  return { view, viewport, flyTo, frame, home: homeView, cancel, onMove, HOME: home };
}

export { HOME as DEFAULT_HOME };
