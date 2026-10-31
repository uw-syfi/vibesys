/**
 * Whether a v2 host's VibeSys checkout can serve this app, asked each time its connection becomes
 * ready and on the user's retry (the handshake). The gateway is shipped by the app and always
 * matches it; the checkout is the host's own and can be older. The answer is the registry's own
 * typed one: `inspect-candidate` reports the checkout too old, or the registry answers UNSUPPORTED
 * (the checkout lacks `inspect-candidate` itself, or speaks another API schema). The client reads
 * no stderr; the gateway reads it only to tell argparse rejecting that one command from any other
 * failure.
 */
import {HOST_UNSUPPORTED_MESSAGE, isHostUnsupported, type RegistrySetup} from '@vibesys/connection';
import type {HostId, HostKey, HostListing} from '@vibesys/hosts';

/** The selection revision of a check no user selection owns. */
const NO_SELECTION = 1n;

export type HostVersion = 'current' | 'out-of-date';

/** `current` or `out-of-date`; any other failure (the host unreachable) rejects: it is no verdict. */
export async function checkHostVersion(
  setup: RegistrySetup,
  host: HostId,
  checkout: string,
  signal: AbortSignal,
): Promise<HostVersion> {
  try {
    const inspected = await setup.inspectCandidate(host, checkout, NO_SELECTION, signal);
    return inspected.state === 'too_old' ? 'out-of-date' : 'current';
  } catch (error) {
    if (isHostUnsupported(error)) return 'out-of-date';
    throw error;
  }
}

/** What a host's version gate needs from its host: the check, and the screen's one status channel. */
export interface HostVersionGateDeps {
  readonly host: HostKey;
  readonly check: (signal: AbortSignal) => Promise<HostVersion>;
  readonly push: (listing: HostListing) => void;
  /** Why a check failed to answer. */
  readonly log: (reason: string) => void;
}

/**
 * Decides which of a connected host's listings reach the screen. A host is shown unless a check
 * said its checkout is out of date; a check that fails or has not answered is no verdict, so it
 * never hides a healthy host. Each check belongs to one connection generation (`recheck` and
 * `invalidate` start a new one and abort the old), and an answer from an older generation is
 * dropped. The latest listing is kept and published when a check clears the host.
 */
export class HostVersionGate {
  readonly #deps: HostVersionGateDeps;
  #outOfDate = false;
  #latest: HostListing | null = null;
  #generation = 0;
  #running: AbortController | null = null;

  constructor(deps: HostVersionGateDeps) {
    this.#deps = deps;
  }

  /** The host's helper listing: shown unless the host is known out of date, and always kept. */
  listing(listing: HostListing): void {
    this.#latest = listing;
    if (!this.#outOfDate) this.#deps.push(listing);
  }

  /** Start the check for a new connection generation (a ready connection, or the user's retry). */
  recheck(): void {
    const generation = this.invalidate();
    const running = new AbortController();
    this.#running = running;
    void this.#deps.check(running.signal).then(
      verdict => this.#settle(generation, verdict),
      (error: unknown) => {
        if (generation !== this.#generation) return;
        this.#deps.log(error instanceof Error ? error.message : String(error));
        this.#settle(generation, 'current');
      },
    );
  }

  /** The connection left ready, or the gate is retired: no answer to a started check applies. */
  invalidate(): number {
    this.#running?.abort();
    this.#running = null;
    return ++this.#generation;
  }

  #settle(generation: number, verdict: HostVersion): void {
    if (generation !== this.#generation) return;
    this.#running = null;
    if (verdict === 'out-of-date') {
      this.#outOfDate = true;
      this.#deps.push({
        kind: 'unavailable',
        host: this.#deps.host,
        step: 'runs',
        connection: {kind: 'incompatible', detail: HOST_UNSUPPORTED_MESSAGE},
      });
      return;
    }
    const hidden = this.#outOfDate;
    this.#outOfDate = false;
    if (hidden && this.#latest !== null) this.#deps.push(this.#latest);
  }
}
