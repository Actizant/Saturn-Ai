/**
 * Saturn's View -- layer registry.
 *
 * A layer is a small object with a cadence and a lifecycle. It does not own a
 * timer, does not know which other layers exist, and cannot reach the globe.
 * This registry owns scheduling, error containment, backoff and freshness
 * reporting on its behalf, so every layer behaves the same way under failure:
 * it says what went wrong and how old its last good sample is.
 *
 * Layer definition:
 *   {
 *     id:      'flights',
 *     title:   'Flights',
 *     group:   'air',
 *     source:  'adsb.lol',          // shown to the user as provenance
 *     cadence: 4000,                // ms between refreshes
 *     create(ctx) -> { start?, stop?, refresh?, dispose? }
 *   }
 */

const TICK_MS = 250;
const MAX_BACKOFF = 8;
/**
 * How long a failing layer waits before its first retry, regardless of cadence.
 *
 * Backoff alone multiplies the cadence, which is right for a layer that is
 * being rate-limited and wrong for one that hit a single upstream 502: a
 * six-hourly element set with an 8x backoff would disappear from the sky for
 * two days. Retries are paced in seconds; the settled cadence resumes the
 * moment the feed answers again.
 */
const RETRY_BASE_MS = 20_000;

export function createLayerRegistry({ bus, CHANNELS, freshness, context }) {
  const definitions = new Map();
  const running = new Map();
  let ticker = null;
  let paused = false;

  function log(...args) {
    console.info('[saturn-view][layers]', ...args);
  }

  function warn(...args) {
    console.warn('[saturn-view][layers]', ...args);
  }

  function schedule(entry) {
    const { def, failures } = entry;
    const backoff = Math.min(MAX_BACKOFF, 2 ** Math.min(failures, 3));
    const base = failures > 0 ? Math.min(def.cadence, RETRY_BASE_MS) : def.cadence;
    entry.dueAt = Date.now() + base * backoff;
  }

  async function refresh(entry, { manual = false } = {}) {
    const { def, handle } = entry;
    if (!handle || entry.inflight) return;
    if (!manual && entry.inflight) return;
    entry.inflight = true;
    try {
      const result = await handle.refresh?.({ viewport: context.viewport() });
      entry.failures = 0;
      // A feed that answered has answered, even with nothing in it. Saying
      // "0 aircraft" is honest; saying "no data" would not be.
      freshness.touch(def.id, {
        source: result?.source || def.source,
        epoch: result?.epoch,
      });
      bus.emit(CHANNELS.LAYER_UPDATED, { id: def.id, count: result?.count ?? 0 });
    } catch (err) {
      entry.failures += 1;
      const reason = err?.message || String(err);
      warn(`${def.id} refresh failed (${entry.failures}): ${reason}`);
      freshness.fail(def.id, reason);
      bus.emit(CHANNELS.LAYER_FAILED, { id: def.id, reason });
    } finally {
      entry.inflight = false;
      schedule(entry);
    }
  }

  function tick() {
    if (paused) return;
    const now = Date.now();
    for (const entry of running.values()) {
      if (now >= entry.dueAt) refresh(entry);
    }
  }

  return {
    register(definition) {
      if (!definition?.id || typeof definition.create !== 'function') {
        throw new Error('layer definition needs an id and create()');
      }
      definitions.set(definition.id, {
        id: definition.id,
        title: definition.title || definition.id,
        group: definition.group || 'other',
        source: definition.source || null,
        cadence: Math.max(1000, definition.cadence || 5000),
        create: definition.create,
      });
      log(`registered ${definition.id}`);
      return this;
    },

    /** Enable a layer: construct it, declare its cadence, refresh immediately. */
    enable(id) {
      if (running.has(id)) return true;
      const def = definitions.get(id);
      if (!def) {
        warn(`cannot enable unknown layer "${id}"`);
        return false;
      }
      // A layer whose useful window moves with the camera -- the city register
      // is the first -- can ask to be refreshed now. It does not get to poll:
      // this only brings the registry's own next refresh forward, so cadence,
      // backoff, error containment and freshness all still apply to it.
      const entry = { def, handle: null, failures: 0, dueAt: 0, inflight: false };
      entry.retrigger = () => {
        if (running.get(def.id) !== entry) return;
        entry.dueAt = 0;
      };

      let handle = null;
      try {
        handle = def.create({ ...context, retrigger: entry.retrigger });
      } catch (err) {
        warn(`${id} failed to construct: ${err?.message || err}`);
        freshness.declare(id, { cadenceMs: def.cadence, source: def.source });
        freshness.fail(id, `construct failed: ${err?.message || err}`);
        return false;
      }
      entry.handle = handle;
      running.set(id, entry);
      freshness.declare(id, { cadenceMs: def.cadence, source: def.source });
      try {
        handle?.start?.({ viewport: context.viewport() });
      } catch (err) {
        warn(`${id} start() threw: ${err?.message || err}`);
      }
      if (!ticker) ticker = setInterval(tick, TICK_MS);
      bus.emit(CHANNELS.LAYER_ENABLED, { id });
      refresh(entry, { manual: true });
      return true;
    },

    disable(id) {
      const entry = running.get(id);
      if (!entry) return false;
      running.delete(id);
      try {
        entry.handle?.dispose?.();
      } catch (err) {
        warn(`${id} dispose() threw: ${err?.message || err}`);
      }
      freshness.off(id);
      bus.emit(CHANNELS.LAYER_DISABLED, { id });
      if (running.size === 0 && ticker) {
        clearInterval(ticker);
        ticker = null;
      }
      return true;
    },

    toggle(id, force) {
      const want = typeof force === 'boolean' ? force : !running.has(id);
      return want ? this.enable(id) : this.disable(id);
    },

    isEnabled(id) {
      return running.has(id);
    },

    setEnabled(ids, enabled = true) {
      const list = Array.isArray(ids) ? ids : [ids];
      return list.map((id) => this.toggle(id, enabled));
    },

    disableAll() {
      for (const id of Array.from(running.keys())) this.disable(id);
    },

    /** Force an immediate refresh outside the cadence. */
    async refreshNow(id) {
      const entry = running.get(id);
      if (!entry) return false;
      await refresh(entry, { manual: true });
      return true;
    },

    /**
     * A layer may publish one line of live text -- "38 aircraft", "ISS in 11m".
     * It is the layer's own figure, never the registry's guess, and a broken
     * readout must never break the rail.
     */
    readout(id) {
      const entry = running.get(id);
      if (!entry?.handle?.readout) return '';
      try {
        return entry.handle.readout() || '';
      } catch (err) {
        warn(`${id} readout() threw: ${err?.message || err}`);
        return '';
      }
    },

    /**
     * A layer's own read-only state. The rail uses the readout; this is for
     * selection panels, the assistant's tool calls, and diagnosis when a layer
     * reports something the globe does not show.
     */
    snapshot(id) {
      const entry = running.get(id);
      if (!entry?.handle) return null;
      try {
        // Called, not returned. This read `return entry.handle.snapshot ??
        // null` and handed back the function itself, which serialises to
        // nothing at all -- so every layer that published a snapshot answered
        // the host with null and looked broken from outside.
        return typeof entry.handle.snapshot === 'function' ? entry.handle.snapshot() : null;
      } catch (err) {
        warn(`${id} snapshot threw: ${err?.message || err}`);
        return null;
      }
    },

    /** Everything the HUD needs to draw a layer rail. */
    list() {
      return Array.from(definitions.entries()).map(([id, def]) => ({
        id,
        title: def.title,
        group: def.group,
        source: def.source,
        cadence: def.cadence,
        enabled: running.has(id),
        freshness: freshness.get(id),
        readout: this.readout(id),
      }));
    },

    /** Counts and failure state, for the status readout. */
    stats() {
      const out = { enabled: [], failing: [] };
      for (const [id, entry] of running) {
        out.enabled.push(id);
        if (entry.failures > 0) out.failing.push({ id, failures: entry.failures });
      }
      out.registered = definitions.size;
      return out;
    },

    /** Stop polling while the page is hidden; resume on return. */
    setPaused(value) {
      paused = Boolean(value);
      if (!paused) {
        const now = Date.now();
        for (const entry of running.values()) entry.dueAt = now;
      }
      log(paused ? 'paused' : 'resumed');
    },

    dispose() {
      this.disableAll();
      definitions.clear();
      if (ticker) {
        clearInterval(ticker);
        ticker = null;
      }
    },
  };
}
