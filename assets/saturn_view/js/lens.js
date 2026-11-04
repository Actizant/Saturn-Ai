/**
 * Saturn's View -- lenses.
 *
 * Same Earth, three ways of looking at it. A lens owns every decision about
 * the *surface* -- whether imagery is fetched, whether relief is worth its
 * cost, what the land is painted with, how loud the geography is -- and no
 * decision about the data. Aircraft, satellites and quakes do not know which
 * lens they are in, and must never need to.
 *
 *   orbit   Dark glass, amber geography, no tiles. The default, because a
 *           photograph of the Earth is a picture of the Earth, while this is
 *           a reading of it. It costs no network per pan and shows no
 *           licensed imagery, so there is no image credit to explain away.
 *
 *   relief  The world as photographed, with real elevation. For the times a
 *           coastline is not the question and the ground is.
 *
 *   signal  Geography pulled back to a whisper and nothing else behind the
 *           feeds. When you stop looking at the Earth and start looking at
 *           what is on it.
 *
 * The orbital shells are the one piece of theatre here that is also true:
 * three equatorial rings at the real radii of LEO, MEO and GEO. Saturn's own
 * rings, made literal -- and a satellite's distance from the centre of the
 * Earth puts it on the ring that governs it. That is why they are worth
 * drawing and why nobody who wraps Cesium gets them for free.
 */

export const LENS_NAMES = Object.freeze(['orbit', 'relief', 'signal']);
export const DEFAULT_LENS = 'orbit';

const STORE_KEY = 'saturn.view.lens';

/** Where the coastlines sit, in metres above the ellipsoid. */
const COAST_LIFT = 800;
const GRID_LIFT = 500;

/**
 * Where the drawn Earth hands over to the real one.
 *
 * Two honest facts decide this window. From orbit, satellite tiles are at zoom
 * 2 and 3: a blur of the planet, where our own vector coastline is the sharper
 * picture -- so the drawing is the subject up there and no tile server should be
 * asked for anything. Come down past a few thousand kilometres and a photograph
 * starts to be a place rather than a smudge, and nothing a fill can do will
 * make it read as rock and water.
 *
 * The lower bound is not arbitrary: it is `TERRAIN_NEAR` in engine.js, the
 * altitude at which real elevation engages. Ground and relief arrive on the
 * same frame instead of one after the other, so the camera comes down onto a
 * planet rather than onto a map of one and then, later, onto a planet.
 *
 * The old near-window argument still holds and is now satisfied far above it:
 * the wash must be gone before the city register, because a grey ground
 * flattens every hairline drawn on it. It is.
 */
const EARTH_KEEP = 5_000_000;
const EARTH_GONE = 1_200_000;

/**
 * The sun's own altitude band, and it is deliberately a different one.
 *
 * A real planet is lit by a real star, which means half of it is in shadow --
 * and a HUD whose night side is black is a HUD that stops working at night.
 * Cesium answers this without any colour hack: the globe ramps its own lighting
 * between `lightingFadeInDistance` and `lightingFadeOutDistance`, so the view
 * from space stays the flat, always-readable instrument it has always been and
 * the terminator arrives by degrees on the way down.
 *
 * The band is set once, in the engine, and left alone: it is a property of how
 * far away the camera is, not of which lens is on screen. What a lens decides
 * is only whether it wants the sun at all. The far edge sits above the hero
 * disc on purpose, so the fade is finished well before the surface hands over
 * at 5,000 km and lighting never competes with the drawing.
 */
const SUN_FAR = 20_000_000;
const SUN_NEAR = 9_000_000;

/**
 * The land wash's own resolution, in a 2:1 equirectangular.
 *
 * This is a fill, not a map: the coastline that the eye actually reads is
 * drawn as geometry over the top of it, so the only thing the pixels have to
 * get right is which side of that line they are on.
 *
 * Both sizes were photographed from the same two cameras, layers off, the wash
 * at the same alpha, and the difference was counted rather than argued. From
 * the orbit disc they are the same picture -- 0.02% of the frame
 * moves at all, because the whole Earth is about 750 pixels across on a
 * 900-pixel-tall frame, a fifth of a degree per screen pixel, and one of these
 * texels is 0.176 of a degree. From 900 km the texel is thirty screen pixels
 * wide and the edge of the fill does feather further away from the drawn line:
 * 4% of that frame, none of it saying anything the amber geometry is not
 * already saying.
 *
 * What halving buys is the half the assistant feels. Two of those numbers are
 * arithmetic rather than measurement and will never change: the canvas is four
 * times smaller, 32 MB to 8, and the jpeg it encodes is 217 KB to 83. The rest
 * was timed with the two builds run back to back, because on this machine the
 * same build re-times by a half -- that pair gave 162 ms to bake and 137 to
 * encode at 4096 against 134 and 80 here, a stall on the page's main thread of
 * 299 ms down to 214. Eighty-five milliseconds of frozen camera is eighty-five
 * milliseconds in which a tool call arriving from Python may go unanswered and
 * get reported as a broken lens, which is the whole argument for the smaller
 * number. The bake moves least because most of it is arithmetic over sixty
 * thousand polygon points rather than over pixels, so anyone repeating that a
 * quarter of the resolution costs a quarter of the time is quoting a figure
 * nobody measured -- including, once, the person who wrote this comment.
 *
 * The aspect has to stay 2:1 -- a 720p-shaped canvas would stretch the poles
 * and put every coastline out of register with the line drawn over it, which
 * is the one error worth avoiding here.
 */
const TEX_WIDTH = 2048;
const TEX_HEIGHT = 1024;

const INK = Object.freeze({
  ocean: '#04070C',
  land: 'rgba(170, 188, 206, 0.20)',
  gold: '#E8B84B',
  halo: '#F2D089',
  mute: '#9FB3C8',
});

/**
 * LEO is where the stations and the imaging constellations are, MEO is where
 * navigation is, GEO is where the belt of comsats sits. The numbers are the
 * real altitudes, not stage dressing: they are what lets an operator check a
 * satellite's shell by eye instead of trusting a label.
 */
const SHELLS = Object.freeze([
  { id: 'LEO', altitude: 550_000 },
  { id: 'MEO', altitude: 20_200_000 },
  { id: 'GEO', altitude: 35_786_000 },
]);

const EARTH_RADIUS = 6_378_137;

/**
 * What each lens does to the surface. Alpha is the only thing that changes at
 * a switch, because rebuilding 60,000 vertices to dim a line is a waste of a
 * frame; the geometry is built once and the light is turned down.
 */
const THEMES = {
  orbit: {
    label: 'Orbit',
    texture: true,
    // The real Earth underneath, the instrument drawn on top of it. Mounted,
    // but hidden above the handoff altitude, so a view from space still asks
    // an imagery service for exactly nothing.
    imagery: 'satellite',
    terrain: true,
    // The sun is on, but ramped by altitude in `governSurface`: full daylight
    // with a terminator down where the ground is, and no shadow at all from
    // where the whole disc is the subject. This is what puts the blue back in
    // the ocean and the sheen where the sun hits it.
    lighting: true,
    // Ground haze is daylight scattering, and daylight is now the point. It
    // used to flood the whole disc and turn a black instrument into a
    // photograph of a grey sea -- but that was with no sun and no photograph,
    // when the only thing haze could land on was the drawing. With the real
    // Earth beneath it, the haze is the air over the land.
    atmosphere: true,
    sky: { hue: -0.52, brightness: -0.22, saturation: 0.3 },
    haze: { brightness: -0.3, hue: 0, saturation: 0 },
    coast: 0.95,
    glow: 0.09,
    grid: 0.075,
    shell: 0.34,
    globe: '#04070C',
    credit: 'Geometry: Natural Earth (public domain)',
    note: 'amber geography over a photographed Earth, three shells',
  },
  relief: {
    label: 'Relief',
    texture: false,
    imagery: 'satellite',
    terrain: true,
    atmosphere: true,
    coast: 0.32,
    glow: 0.05,
    grid: 0.035,
    shell: 0.1,
    globe: '#070B12',
    sky: { hue: 0, brightness: 0, saturation: 0 },
    haze: { brightness: -0.3, hue: 0, saturation: 0 },
    credit: null,
    note: 'photographed, with real elevation',
  },
  signal: {
    label: 'Signal',
    texture: false,
    imagery: null,
    terrain: false,
    atmosphere: false,
    coast: 0.5,
    glow: 0.07,
    grid: 0.15,
    shell: 0.45,
    globe: '#04060A',
    sky: { hue: -0.52, brightness: -0.42, saturation: 0.3 },
    haze: { brightness: 0, hue: 0, saturation: 0 },
    credit: 'Geometry: Natural Earth (public domain)',
    note: 'geography to a whisper, feeds to the front',
  },
};

/** Group id to the theme field that lights it. */
const THEME_KEY = Object.freeze({
  coast: 'coast',
  glow: 'glow',
  grid: 'grid',
  shells: 'shell',
});

function readStored() {
  try {
    const saved = window.localStorage.getItem(STORE_KEY);
    return THEMES[saved] ? saved : null;
  } catch (err) {
    return null;
  }
}

function writeStored(name) {
  try {
    window.localStorage.setItem(STORE_KEY, name);
  } catch (err) {
    /* A private profile with storage disabled is not a reason to fail. */
  }
}

export function createLens(options) {
  const {
    CESIUM,
    viewer,
    scene,
    camera,
    clock,
    bus,
    CHANNELS,
    creditsLine,
    imageryProvider,
    setTerrainAllowed,
    imageryCredit,
    terrain,
    requestRefresh,
  } = options;

  let current = readStored() || DEFAULT_LENS;
  let land = null;
  let landRequest = null;
  /** The land fill, as an imagery layer: one global image, mounted or not. */
  let landLayer = null;
  let disposed = false;
  let lastTerrainState = terrain ? terrain.state : 'unsupported';

  /** Every drawn group, so a switch only has to move alpha. */
  const groups = {
    coast: null,
    glow: null,
    grid: null,
    shells: null,
  };

  const shellLabels = [];
  let unsubscribe = null;

  // ------------------------------------------------------------------ land //

  function fetchLand() {
    if (land) return Promise.resolve(land);
    if (landRequest) return landRequest;

    landRequest = fetch('/api/land', { cache: 'force-cache' })
      .then((response) => {
        if (!response.ok) throw new Error(`land feed ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        const polygons = Array.isArray(payload.polygons) ? payload.polygons : [];
        land = polygons.filter((polygon) => Array.isArray(polygon) && polygon.length);
        if (!land.length) throw new Error('land feed was empty');
        console.info(`[saturn-view][lens] land ready: ${land.length} polygons`);
        return land;
      })
      .catch((err) => {
        // No coastline is a worse view, not a broken one. The shells and the
        // feeds still work; say so rather than throwing the lens away.
        console.warn(`[saturn-view][lens] no land geometry: ${err.message || err}`);
        land = [];
        return land;
      });

    return landRequest;
  }

  /**
   * Land and sea baked into one opaque equirectangular image, then delivered
   * as an imagery layer rather than as `globe.material`.
   *
   * Both were tried, and this is the one that worked. `globe.material` with an
   * image texture misprojects: the whole world map is drawn flat across the
   * frame instead of wrapping the ellipsoid. The same image as an imagery
   * layer wrapped correctly on the first attempt, because that is the path the
   * renderer actually exercises. Opaque rather than transparent for the same
   * reason -- bake the sea in, and what was drawn is what is seen.
   */
  function paintLand(polygons) {
    const canvas = document.createElement('canvas');
    canvas.width = TEX_WIDTH;
    canvas.height = TEX_HEIGHT;

    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) return null;

    ctx.fillStyle = INK.ocean;
    ctx.fillRect(0, 0, TEX_WIDTH, TEX_HEIGHT);
    ctx.fillStyle = INK.land;

    const project = (lon, lat) => [
      ((lon + 180) / 360) * TEX_WIDTH,
      ((90 - lat) / 180) * TEX_HEIGHT,
    ];

    for (const polygon of polygons) {
      let west = 180;
      let east = -180;

      for (const ring of polygon) {
        for (const point of ring) {
          if (point[0] < west) west = point[0];
          if (point[0] > east) east = point[0];
        }
      }

      // A polygon that touches the seam has to be drawn again on the far side,
      // or Antarctica and the Pacific islands get a vertical cut through them.
      const offsets = [0];
      if (west < -175) offsets.push(TEX_WIDTH);
      if (east > 175) offsets.push(-TEX_WIDTH);

      for (const offset of offsets) {
        ctx.beginPath();
        for (const ring of polygon) {
          for (let i = 0; i < ring.length; i += 1) {
            const [x, y] = project(ring[i][0] + offset, ring[i][1]);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.closePath();
        }
        // even-odd fills holes as holes, which is what a lake in a continent is.
        ctx.fill('evenodd');
      }
    }

    return canvas;
  }

  /**
   * Mount the land image as the globe's own imagery layer.
   *
   * One tile for the whole world is coarse -- twenty kilometres or so a pixel
   * -- which is exactly why the fill is a whisper and the coastline is drawn
   * over it as geometry. The blur sits behind a crisp edge, so it reads as a
   * halo rather than as a low-resolution map.
   */
  function mountLand(polygons) {
    const t0 = performance.now();
    const canvas = paintLand(polygons);
    if (!canvas) return null;
    const baked = performance.now() - t0;

    let url;
    try {
      // JPEG, not PNG, and not for size. Encoding a canvas of this one as a
      // PNG takes the page's main thread for the better part of a second,
      // which is long enough for a tool call arriving from Python to time out
      // and be reported as a failed layer switch. There is nothing in this
      // image but a soft fill, so the lossy format costs nothing the eye can
      // find.
      url = canvas.toDataURL('image/jpeg', 0.9);
    } catch (err) {
      console.warn(`[saturn-view][lens] land image failed: ${err.message || err}`);
      return null;
    }

    // Both halves of the cost are named, because they fail differently: the
    // bake is arithmetic over sixty thousand points and the encode is a
    // lossy compression of a bitmap neither of which can be interrupted. The
    // whole thing runs on the page's main thread, so the milliseconds here
    // are milliseconds of frozen camera, and a tool call from Python that
    // arrives during them times out and gets reported as a broken lens.
    const encode = performance.now() - t0 - baked;
    const pixels = (canvas.width * canvas.height * 4) / 1048576;
    console.info(
      `[saturn-view][lens] land image ${canvas.width}x${canvas.height}, ` +
      `${pixels.toFixed(1)} MB canvas, ${(url.length * 0.75 / 1024).toFixed(0)} KB jpeg, ` +
      `baked ${Math.round(baked)} ms, encoded ${Math.round(encode)} ms`
    );

    landLayer = new CESIUM.ImageryLayer(
      new CESIUM.SingleTileImageryProvider({
        url,
        tileWidth: TEX_WIDTH,
        tileHeight: TEX_HEIGHT,
        tilingScheme: new CESIUM.GeographicTilingScheme(),
        rectangle: CESIUM.Rectangle.fromDegrees(-180, -90, 180, 90),
      })
    );

    viewer.imageryLayers.add(landLayer);
    landLayer.show = Boolean(THEMES[current].texture);
    return landLayer;
  }

  /**
   * Imagery is the one part of a lens that is not a colour: either photographs
   * are fetched or they are not. The satellite layer is destroyed when a lens
   * stops wanting it -- a hidden layer still holds a tile cache -- while the
   * land image is kept and simply switched off.
   *
   * The mounted layer is remembered, because `governSurface` has to fade it
   * against the land wash, and a layer nobody holds a handle to cannot be
   * faded, only re-created every frame.
   */
  let photoLayer = null;

  // Declared with the layer they belong to rather than with the function that
  // fades them: `applyLayers` seeds both on every switch, and a `let` it
  // reaches backwards to is a trap that only springs when the call order moves.
  let washAlpha = 1;
  let photoAlpha = 0;
  // Last strength written to each faded line group, so a descent that changes
  // the alpha by a thousandth every frame does not re-issue the material that
  // all 1,400 coastlines share.
  let coastFade = -1;
  let glowFade = -1;

  function applyLayers(theme) {
    const layers = viewer.imageryLayers;

    for (let i = layers.length - 1; i >= 0; i -= 1) {
      const layer = layers.get(i);
      if (layer !== landLayer) layers.remove(layer, true);
    }
    photoLayer = null;

    if (theme.imagery) photoLayer = layers.addImageryProvider(imageryProvider(theme.imagery)) || null;
    if (landLayer) landLayer.show = Boolean(theme.texture);

    // A lens that draws its own ground starts with the photograph fully away,
    // which also keeps it from being fetched at all until the camera descends.
    photoAlpha = theme.texture ? 0 : 1;
    if (photoLayer) {
      photoLayer.alpha = photoAlpha;
      photoLayer.show = photoAlpha > 0.002;
    }
  }

  /**
   * Fade a line group by rewriting the one material its thousands of polylines
   * share, and switch the whole data source off once it is invisible.
   *
   * Quantised to 1/64 because a change of alpha on a shared material fires a
   * definition-changed event, and 1,400 polylines re-resolving their material
   * on a frame where nothing moved is a stall spent on a difference no eye
   * can see. Hiding the source at zero is not cosmetic either: an invisible
   * polyline collection is still a collection the renderer walks.
   */
  function fadeGroup(id, scale, last) {
    const group = groups[id];
    if (!group) return last;
    const wanted = Math.round(Math.min(1, Math.max(0, group.base * scale)) * 64) / 64;
    if (wanted === last) return last;
    // Cesium's `Color` has a settable `alpha` and no `setValue` -- calling one
    // is how this function threw on every tick, which did not merely log: the
    // clock listener died before the camera was advanced, so the globe never
    // left the altitude it opened at. A fresh Color per change costs nothing
    // (this runs a few dozen times across a descent, not per frame) and cannot
    // corrupt the shared base by accident.
    const base = group.baseColour;
    group.material.color = new CESIUM.Color(base.red, base.green, base.blue, wanted);
    const show = wanted > 0.002;
    if (group.source.show !== show) group.source.show = show;
    return wanted;
  }

  /**
   * How much of the drawn surface this lens is currently showing.
   *
   * Shared by the tick and by a lens switch, because both have to answer the
   * same question and only one of them gets to ask it every frame. A lens with
   * no drawn ground has nothing to fade and stays at one; a lens that has not
   * yet been told where the camera is stays at one too, which is the setting
   * that asks no imagery service for anything.
   */
  function surfaceWash(theme) {
    if (!theme.texture) return 1;
    const height = camera.view()?.height;
    if (!Number.isFinite(height)) return 1;
    return Math.min(1, Math.max(0, (height - EARTH_GONE) / (EARTH_KEEP - EARTH_GONE)));
  }

  /**
   * The surface, faded by altitude.
   *
   * A photograph of the ground and a drawing of the ground cannot both be the
   * subject at once, so the two alphas are one number read from opposite ends:
   * whatever the drawing has, the photograph does not. The guard on the
   * previous value earns its place twice over -- assigning an unchanged alpha
   * to an imagery layer every tick keeps the tile queue awake for nothing, and
   * leaving a fully transparent layer *shown* would have Cesium fetch tiles
   * that nobody can see, which is the exact cost this window exists to avoid.
   */
  function governSurface() {
    const theme = THEMES[current];
    const height = camera.view()?.height;

    const wash = surfaceWash(theme);

    let dirty = false;

    if (landLayer && wash !== washAlpha) {
      washAlpha = wash;
      landLayer.alpha = wash;
      dirty = true;
    }

    // The amber coastline is part of the drawing, so it goes when the drawing
    // goes. Left behind over a photograph it would be an outline of a coast on
    // top of the coast it outlines -- and at the altitudes where the wash is
    // gone, the real shoreline is sharper than anything drawn over it.
    coastFade = fadeGroup('coast', wash, coastFade);
    glowFade = fadeGroup('glow', wash, glowFade);

    // Three cases, and only the last one is a blend. A lens with no imagery has
    // nothing to fade into; a lens that draws nothing has nothing to fade *out*;
    // and before the camera has reported anywhere, the safe answer is the one
    // that asks no service for anything.
    let photo = 1;
    if (photoLayer) {
      if (!theme.texture) photo = 1;
      else if (!Number.isFinite(height)) photo = 0;
      else photo = 1 - wash;

      if (photo !== photoAlpha) {
        photoAlpha = photo;
        photoLayer.alpha = photo;
        const show = photo > 0.002;
        if (photoLayer.show !== show) photoLayer.show = show;
        dirty = true;
      }
    }

    if (dirty) requestRefresh();
  }

  function positions(ring, lift) {
    const flat = new Array(ring.length * 3);
    for (let i = 0; i < ring.length; i += 1) {
      flat[i * 3] = ring[i][0];
      flat[i * 3 + 1] = ring[i][1];
      flat[i * 3 + 2] = lift;
    }
    return CESIUM.Cartesian3.fromDegreesArrayHeights(flat);
  }

  /**
   * One data source, one shared material instance, many lines.
   *
   * The shared instance matters: Cesium batches polyline entities into one
   * collection per material *identity*, so 1,400 coastlines sharing one
   * ColorMaterialProperty are one draw call, and 1,400 identical-yet-separate
   * ones are 1,400. This is the whole reason the Orbit lens can afford a
   * 60,000-point coastline.
   */
  function drawGroup(id, rings, { colour, alpha, width, base }) {
    const source = new CESIUM.CustomDataSource(`lens-${id}`);
    const material = new CESIUM.ColorMaterialProperty(
      CESIUM.Color.fromCssColorString(colour).withAlpha(alpha)
    );

    for (const ring of rings) {
      source.entities.add({
        polyline: {
          positions: ring,
          width,
          material,
          // Straight segments between measured points: the points are dense
          // enough that a geodesic resample would only cost time.
          arcType: CESIUM.ArcType.NONE,
        },
      });
    }

    viewer.dataSources.add(source);
    groups[id] = {
      source,
      material,
      // The colour as the theme asked for it, alpha included, never written to.
      baseColour: CESIUM.Color.fromCssColorString(colour).withAlpha(alpha),
      colour: CESIUM.Color.fromCssColorString(colour),
      // The theme's own strength, kept so a fade is relative to what the lens
      // asked for rather than to full opacity: the glow halo is faint at best
      // and must not be made louder by being dimmed.
      base: Number.isFinite(base) ? base : alpha,
      width,
    };
    return groups[id];
  }

  function graticuleRings() {
    const rings = [];

    for (let lon = -180; lon < 180; lon += 15) {
      const ring = [];
      for (let lat = -80; lat <= 80; lat += 2) ring.push([lon, lat]);
      rings.push(positions(ring, GRID_LIFT));
    }

    for (let lat = -75; lat <= 75; lat += 15) {
      const ring = [];
      for (let lon = -180; lon <= 180; lon += 2) ring.push([lon, lat]);
      rings.push(positions(ring, GRID_LIFT));
    }

    return rings;
  }

  function shellRings() {
    const rings = [];

    for (const shell of SHELLS) {
      const radius = EARTH_RADIUS + shell.altitude;
      const points = [];
      for (let degree = 0; degree < 360; degree += 1) {
        const theta = (degree * Math.PI) / 180;
        points.push(
          new CESIUM.Cartesian3(radius * Math.cos(theta), radius * Math.sin(theta), 0)
        );
      }
      rings.push(points);
    }

    return rings;
  }

  /**
   * Shell names ride the near side of their ring, so the label is always
   * legible and always attached to the thing it names. Three positions at
   * ten hertz is cheaper than a label that is only correct at one heading.
   */
  function addShellLabels() {
    const colour = CESIUM.Color.fromCssColorString(INK.gold).withAlpha(0.7);
    const source = groups.shells.source;

    for (const shell of SHELLS) {
      const entity = source.entities.add({
        position: CESIUM.Cartesian3.fromDegrees(0, 0, shell.altitude),
        label: {
          text: shell.id,
          font: '10px "Cascadia Mono", Consolas, monospace',
          fillColor: colour,
          style: CESIUM.LabelStyle.FILL,
          scale: 1,
          showBackground: false,
          translucencyByDistance: new CESIUM.NearFarScalar(
            4_000_000,
            1,
            40_000_000,
            0.25
          ),
        },
      });
      shellLabels.push({ entity, altitude: shell.altitude, id: shell.id });
    }
  }

  function trackShellLabels() {
    const at = camera.view();
    if (!at || !Number.isFinite(at.lon)) return;
    const lon = at.lon + 68;

    for (const item of shellLabels) {
      item.entity.position = CESIUM.Cartesian3.fromDegrees(lon, 0, item.altitude);
    }
  }

  // -------------------------------------------------------------- geometry //

  let building = null;

  function ensureGeometry() {
    if (building) return building;
    if (groups.coast) return Promise.resolve(true);

    building = fetchLand().then((polygons) => {
      if (disposed) return false;
      if (!polygons.length) return false;

      const rings = [];
      for (const polygon of polygons) {
        for (const ring of polygon) rings.push(positions(ring, COAST_LIFT));
      }

      const theme = THEMES[current];

      mountLand(polygons);

      drawGroup('glow', rings, {
        colour: INK.halo,
        alpha: theme.glow,
        width: 3,
        base: theme.glow,
      });
      drawGroup('coast', rings, {
        colour: INK.gold,
        alpha: theme.coast,
        width: 1.3,
        base: theme.coast,
      });
      drawGroup('grid', graticuleRings(), {
        colour: INK.mute,
        alpha: theme.grid,
        width: 1,
      });
      drawGroup('shells', shellRings(), {
        colour: INK.gold,
        alpha: theme.shell,
        width: 1.5,
      });
      addShellLabels();

      requestRefresh();
      return true;
    });

    return building;
  }

  // ------------------------------------------------------------------ paint //

  function paintCredits(theme) {
    if (!creditsLine) return;

    const parts = [];
    if (theme.imagery) parts.push(imageryCredit);

    // Only claim terrain when terrain is what is on screen. A credit for a
    // service that is not being called is the same lie as a missing one.
    if (theme.terrain && terrain && (terrain.state === 'live' || terrain.active)) {
      parts.push(terrain.credit);
    }

    if (theme.credit) parts.push(theme.credit);
    parts.push('Renderer: CesiumJS');

    const text = parts.filter(Boolean).join(' \u00b7 ');
    if (creditsLine.textContent !== text) creditsLine.textContent = text;
  }

  function tune(name) {
    const theme = THEMES[name];

    scene.globe.showGroundAtmosphere = theme.atmosphere;
    scene.globe.baseColor = CESIUM.Color.fromCssColorString(theme.globe);

    // The sun is a lens decision; the altitude band it fades across is not.
    // `show` on the Sun object is the light itself -- the terminator on the
    // ground and the sheen on the ocean. The disc is a separate, much louder
    // thing and stays off: a lens flare in an instrument window is noise.
    scene.globe.enableLighting = Boolean(theme.lighting);
    if (scene.sun) scene.sun.show = Boolean(theme.lighting);

    const haze = theme.haze || {};
    // These are Globe setters in this build; on a build that lacks one this is
    // an unused own-property rather than a crash, which is the right failure.
    scene.globe.atmosphereBrightnessShift = haze.brightness ?? 0;
    scene.globe.atmosphereHueShift = haze.hue ?? 0;
    scene.globe.atmosphereSaturationShift = haze.saturation ?? 0;

    // The sky atmosphere is the limb: a thin band of scattered light at the
    // edge of the disc, and nothing over the face. That is the whole reason it
    // survives when the ground haze does not.
    //
    // Hue is in *turns*, not degrees. Cesium reads it in the shader as
    // `hsb.x += hsbShift.x` on a hue already normalised to 0..1, so -175 --
    // the degrees that would take blue to amber -- means minus 175 full
    // revolutions, which wraps to no change at all. Blue stayed blue until
    // this was read out of the shader instead of assumed. -0.52 of a turn is
    // the -187 degrees that lands amber.
    const sky = theme.sky;
    if (scene.skyAtmosphere) {
      scene.skyAtmosphere.show = Boolean(sky);
      if (sky) {
        scene.skyAtmosphere.hueShift = sky.hue ?? 0;
        scene.skyAtmosphere.brightnessShift = sky.brightness ?? 0;
        scene.skyAtmosphere.saturationShift = sky.saturation ?? 0;
      }
    }

    for (const [id, group] of Object.entries(groups)) {
      if (!group) continue;
      const alpha = theme[THEME_KEY[id]];
      if (typeof alpha !== 'number') continue;
      // A copy, deliberately: `withAlpha` mutates its receiver, and `colour` is
      // the working colour `fadeGroup` writes through. Handing the material a
      // fresh Color keeps the base intact across lens switches.
      group.baseColour = CESIUM.Color.clone(group.colour, new CESIUM.Color()).withAlpha(alpha);
      group.material.color = CESIUM.Color.clone(group.baseColour, new CESIUM.Color());
    }

    // The theme's coast and glow strengths are what the lens wants *at full
    // draw*. The altitude governor then decides how much of that it actually
    // gets, and has to speak last or a lens switch would shout the coastline
    // back over a photograph -- and reset the fade's memory, since the value
    // it is comparing against has just been written behind its back.
    const wash = surfaceWash(theme);
    coastFade = fadeGroup('coast', wash, -1);
    glowFade = fadeGroup('glow', wash, -1);

    if (groups.shells) {
      for (const item of shellLabels) {
        item.entity.label.fillColor = CESIUM.Color.fromCssColorString(INK.gold).withAlpha(
          Math.min(1, theme.shell + 0.36)
        );
      }
    }

    // The land image carries the sea as well as the land, so Signal switches
    // it off rather than dimming it: a black sea and a black land are the
    // honest version of "show me the data, not the ground". Relief switches it
    // off because photographed ground is the point.
    applyLayers(theme);

    paintCredits(theme);
    requestRefresh();
  }

  // ------------------------------------------------------------------- API //

  function set(name, { announce = true, persist = true } = {}) {
    const wanted = THEMES[name] ? name : DEFAULT_LENS;
    const theme = THEMES[wanted];
    const previous = current;
    current = wanted;

    if (persist) writeStored(wanted);

    // Terrain is a distance decision the engine owns; the lens only decides
    // whether relief is on the menu at all. Imagery is handled with the rest
    // of the surface in `tune`.
    setTerrainAllowed(theme.terrain);

    tune(wanted);
    ensureGeometry();

    if (announce) {
      bus.emit(CHANNELS.LENS_CHANGED, {
        lens: wanted,
        previous,
        label: theme.label,
        note: theme.note,
      });
      console.info(`[saturn-view][lens] ${theme.label}`);
    }

    return { lens: wanted, label: theme.label, note: theme.note };
  }

  function cycle(step = 1) {
    const index = LENS_NAMES.indexOf(current);
    const next = LENS_NAMES[(index + step + LENS_NAMES.length) % LENS_NAMES.length];
    return set(next);
  }

  function describe() {
    const theme = THEMES[current];
    const count = land ? land.reduce((total, polygon) => total + polygon.length, 0) : 0;
    const geometry = count
      ? `${count} coastline rings`
      : land
        ? 'no coastline geometry'
        : 'coastline still loading';
    return `${theme.label} lens: ${theme.note}. ${geometry}, three orbital shells at LEO, MEO and GEO.`;
  }

  unsubscribe = clock.subscribe(() => {
    trackShellLabels();
    governSurface();

    // Terrain arrives after boot, so the relief credit has to be able to
    // correct itself once it is actually true.
    if (terrain && terrain.state !== lastTerrainState) {
      lastTerrainState = terrain.state;
      if (THEMES[current].terrain) paintCredits(THEMES[current]);
    }
  });

  /** Camera moves keep the shell names on the near side of their rings. */
  const offCamera = bus.on(CHANNELS.CAMERA_MOVED, trackShellLabels);

  function dispose() {
    disposed = true;
    if (unsubscribe) unsubscribe();
    offCamera();
    for (const group of Object.values(groups)) {
      if (group?.source) viewer.dataSources.remove(group.source, true);
    }
    shellLabels.length = 0;
    if (landLayer) {
      viewer.imageryLayers.remove(landLayer, true);
      landLayer = null;
    }
  }

  return {
    name: () => current,
    names: () => LENS_NAMES.slice(),
    themes: () => Object.fromEntries(LENS_NAMES.map((id) => [id, THEMES[id].note])),
    set,
    cycle,
    describe,
    ready: () => Boolean(groups.coast),
    dispose,
  };
}
