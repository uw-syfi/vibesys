/**
 * An opt-in amplifier for the Fake welcome bridge: a seeded random delay on the reply of every
 * request method (every method whose name does not start with `on`, which only subscribes).
 * It changes how the page's work interleaves with a spec's inputs, which CPU load and throttling
 * do not, so a spec that acts before the page has read what it shows fails under it on every
 * seed. `E2E_JITTER_SEED=<n> pnpm e2e` turns it on for every world (`addInitScripts` appends it
 * after the Fakes, so it wraps their methods); `E2E_JITTER_MS` sets the longest delay (300).
 */

/** The seed of the delays and the longest one, in milliseconds. */
export interface BridgeJitter {
  readonly seed: number;
  readonly maxMs: number;
}

const DEFAULT_MAX_MS = 300;

/** The amplifier the environment asks for, or null when it asks for none. */
export function bridgeJitterFrom(
  env: Readonly<Record<string, string | undefined>>,
): BridgeJitter | null {
  const seed = env['E2E_JITTER_SEED'];
  if (seed === undefined || seed === '') return null;
  const maxMs = env['E2E_JITTER_MS'];
  const jitter = {
    seed: Number(seed),
    maxMs: maxMs === undefined || maxMs === '' ? DEFAULT_MAX_MS : Number(maxMs),
  };
  if (!Number.isInteger(jitter.seed) || !Number.isInteger(jitter.maxMs) || jitter.maxMs < 0) {
    throw new Error(
      `E2E_JITTER_SEED and E2E_JITTER_MS must be integers (got ${seed}, ${maxMs ?? 'unset'})`,
    );
  }
  return jitter;
}

/**
 * Runs in the page (it is serialized by `initScript`, so it is self-contained): wraps each request
 * method of `window.vibesysWelcome` so its reply comes after a delay drawn from `config.seed`.
 */
export function installBridgeJitter(config: BridgeJitter): void {
  let state = config.seed >>> 0;
  const draw = (): number => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const bridge = (window as unknown as {vibesysWelcome?: Record<string, unknown>}).vibesysWelcome;
  // A world with no welcome bridge (a run window over the gateway alone) has nothing to delay.
  if (bridge === undefined) return;
  for (const [name, method] of Object.entries(bridge)) {
    if (typeof method !== 'function' || name.startsWith('on')) continue;
    bridge[name] = (...args: unknown[]) =>
      new Promise(resolve => {
        const wait = Math.floor(draw() * config.maxMs);
        setTimeout(() => resolve((method as (...a: unknown[]) => unknown)(...args)), wait);
      });
  }
}
