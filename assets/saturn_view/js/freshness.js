/**
 * Saturn's View -- freshness.
 *
 * The one rule this module exists to enforce: the view always knows how old
 * its own eyes are, and says so. Every layer reports the moment its data
 * actually arrived; nothing here estimates, guesses or smooths.
 *
 * A layer is LIVE while its newest sample is inside its own cadence plus
 * tolerance. Past that it is LATE, then STALE, then LOST, and each of those
 * is a visible statement rather than a silent blank.
 */

export const FRESHNESS = Object.freeze({
  OFF: 'off',
  LIVE: 'live',
  LATE: 'late',
  STALE: 'stale',
  LOST: 'lost',
});

/** Multiples of a layer's own cadence that define each band. */
const LATE_AFTER = 1.75;
const STALE_AFTER = 4;
const LOST_AFTER = 12;

function bandFor(ageMs, cadenceMs) {
  if (!Number.isFinite(ageMs)) return FRESHNESS.OFF;
  const cadence = Number.isFinite(cadenceMs) && cadenceMs > 0 ? cadenceMs : 5000;
  if (ageMs <= cadence * LATE_AFTER) return FRESHNESS.LIVE;
  if (ageMs <= cadence * STALE_AFTER) return FRESHNESS.LATE;
  if (ageMs <= cadence * LOST_AFTER) return FRESHNESS.STALE;
  return FRESHNESS.LOST;
}

/**
 * Format an age the way a person reads a clock: "3s", "47s", "4m", "2h".
 * Deliberately blunt -- "just now" and "moments ago" hide the number.
 */
export function formatAge(ageMs) {
  if (!Number.isFinite(ageMs) || ageMs < 0) return '--';
  const seconds = Math.round(ageMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

export function createFreshness({ bus, CHANNELS, now = () => Date.now() }) {
  /** id -> { cadence, receivedAt, source, epoch, note } */
  const rows = new Map();
  const subscribers = new Set();

  function publish() {
    const snap = snapshot();
    for (const fn of Array.from(subscribers)) {
      try {
        fn(snap);
      } catch (err) {
        console.error('[saturn-view][freshness] subscriber failed', err);
      }
    }
    bus?.emit(CHANNELS?.FRESHNESS, snap);
  }

  function snapshot() {
    const at = now();
    const out = {};
    for (const [id, row] of rows) {
      const ageMs = row.receivedAt == null ? Infinity : at - row.receivedAt;
      out[id] = {
        id,
        ageMs,
        age: formatAge(ageMs),
        cadenceMs: row.cadence,
        state: row.receivedAt == null ? FRESHNESS.OFF : bandFor(ageMs, row.cadence),
        source: row.source || null,
        epoch: row.epoch || 0,
        note: row.note || null,
      };
    }
    return out;
  }

  return {
    /** Declare a layer's cadence before its first sample arrives. */
    declare(id, { cadenceMs, source }) {
      const row = rows.get(id) || {};
      rows.set(id, { ...row, cadence: cadenceMs, source, receivedAt: row.receivedAt ?? null });
      publish();
    },

    /** A layer received real data. `epoch` is the upstream's own timestamp when it has one. */
    touch(id, { source, epoch } = {}) {
      const row = rows.get(id) || { cadence: 5000 };
      row.receivedAt = now();
      if (source) row.source = source;
      if (Number.isFinite(epoch)) row.epoch = epoch;
      row.note = null;
      rows.set(id, row);
      publish();
    },

    /** A layer tried and failed. Keeps the last good age, adds the reason. */
    fail(id, reason) {
      const row = rows.get(id) || { cadence: 5000, receivedAt: null };
      row.note = String(reason || 'unavailable');
      rows.set(id, row);
      publish();
    },

    /** A layer was switched off: it has no age at all, and must not look empty. */
    off(id) {
      const row = rows.get(id);
      if (!row) return;
      row.receivedAt = null;
      row.note = null;
      rows.set(id, row);
      publish();
    },

    forget(id) {
      rows.delete(id);
      publish();
    },

    get(id) {
      return snapshot()[id] || null;
    },

    snapshot,

    subscribe(fn) {
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    },

    /** Called on a slow tick so ages keep counting even when nothing arrives. */
    tick: publish,
  };
}
