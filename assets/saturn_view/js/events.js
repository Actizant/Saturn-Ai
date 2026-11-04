/**
 * Saturn's View -- event bus.
 *
 * Everything in the engine talks through here. Layers never call each other
 * directly, and the HUD never reaches into a layer; both subscribe by name.
 * A throwing listener is contained so one broken layer cannot stop the frame.
 */

const CHANNELS = Object.freeze({
  READY: 'engine:ready',
  FRAME: 'engine:frame',
  LAYER_ENABLED: 'layer:enabled',
  LAYER_DISABLED: 'layer:disabled',
  LAYER_UPDATED: 'layer:updated',
  LAYER_FAILED: 'layer:failed',
  FRESHNESS: 'freshness:changed',
  CAMERA_MOVED: 'camera:moved',
  LENS_CHANGED: 'engine:lens',
  SELECTION: 'selection:changed',
});

export function createBus() {
  const listeners = new Map();

  function channel(name) {
    let set = listeners.get(name);
    if (!set) {
      set = new Set();
      listeners.set(name, set);
    }
    return set;
  }

  return {
    /** Subscribe. Returns an unsubscribe function. */
    on(name, fn) {
      if (typeof fn !== 'function') return () => {};
      const set = channel(name);
      set.add(fn);
      return () => set.delete(fn);
    },

    once(name, fn) {
      const off = this.on(name, (payload) => {
        off();
        fn(payload);
      });
      return off;
    },

    emit(name, payload) {
      const set = listeners.get(name);
      if (!set || set.size === 0) return;
      for (const fn of Array.from(set)) {
        try {
          fn(payload);
        } catch (err) {
          console.error(`[saturn-view][bus] listener failed on "${name}"`, err);
        }
      }
    },

    clear() {
      listeners.clear();
    },
  };
}

export { CHANNELS };
