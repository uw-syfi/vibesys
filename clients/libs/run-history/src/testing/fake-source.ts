/**
 * A host's history as an in-memory source: journals served page by page the way the helper serves
 * `runs.read_events` (up to `max` events, cut earlier at a byte bound, never empty before the end,
 * `complete` at the last event), with a log of every request so a test can say how often the host
 * was asked. A read can be held until released, to test reads that overlap, and a read can fail.
 */

import type {HistoryKinds, HistoryRead, HistoryRunKey, HistorySource} from '../store.js';

/** A recorded event as the fake serves it: only its sequence matters to paging. */
export interface FakeEvent {
  readonly sequence: number;
  readonly [field: string]: unknown;
}

export interface FakeSourceOptions {
  /** The byte bound of a page (the length of its events' JSON); a page holds one event at least. */
  readonly pageBytes?: number;
  /** Whether replies name the journal's size, as a host with `run_history_pages` does. */
  readonly withTotals?: boolean;
}

export interface FakeRequest {
  readonly run: HistoryRunKey;
  readonly from: number;
  readonly max: number;
  readonly kinds?: HistoryKinds;
}

function keyOf(run: HistoryRunKey): string {
  return JSON.stringify([run.host, run.project, run.runId]);
}

export class FakeHistorySource implements HistorySource {
  /** Every read asked of the host, in order. */
  readonly requests: FakeRequest[] = [];
  readonly #journals = new Map<string, readonly FakeEvent[]>();
  readonly #options: FakeSourceOptions;
  readonly #kinds = new Map<HistoryKinds, (event: FakeEvent) => boolean>();
  #held: Promise<void> | null = null;
  #release: () => void = () => undefined;
  #failure: Error | null = null;
  #unsupported = false;

  constructor(options: FakeSourceOptions = {}) {
    this.#options = options;
  }

  /** Serve `events` (sequences from 1, increasing) as `run`'s journal. */
  setJournal(run: HistoryRunKey, events: readonly FakeEvent[]): void {
    this.#journals.set(keyOf(run), events);
  }

  /**
   * Serve only the events `selects` keeps to reads of `kinds`, as a host that selects by kind does;
   * a read of kinds not set here is served every event.
   */
  setKind(kinds: HistoryKinds, selects: (event: FakeEvent) => boolean): void {
    this.#kinds.set(kinds, selects);
  }

  /** Answer every read as a host without history does. */
  setUnsupported(unsupported: boolean): void {
    this.#unsupported = unsupported;
  }

  /** The next read fails with `error`. */
  failNext(error: Error): void {
    this.#failure = error;
  }

  /** Hold every read until `release`. */
  hold(): void {
    this.#held = new Promise<void>(resolve => {
      this.#release = resolve;
    });
  }

  release(): void {
    this.#release();
    this.#held = null;
  }

  async read(
    run: HistoryRunKey,
    from: number,
    max: number,
    kinds?: HistoryKinds,
  ): Promise<HistoryRead> {
    this.requests.push({run, from, max, ...(kinds === undefined ? {} : {kinds})});
    if (this.#held !== null) await this.#held;
    if (this.#failure !== null) {
      const failure = this.#failure;
      this.#failure = null;
      throw failure;
    }
    if (this.#unsupported) return {kind: 'unsupported'};
    const whole = this.#journals.get(keyOf(run)) ?? [];
    const selects = kinds === undefined ? undefined : this.#kinds.get(kinds);
    const journal = selects === undefined ? whole : whole.filter(selects);
    const rest = journal.filter(event => event.sequence >= from);
    const page: FakeEvent[] = [];
    let bytes = 0;
    for (const event of rest) {
      if (page.length >= max) break;
      const size = JSON.stringify(event).length;
      const bound = this.#options.pageBytes;
      if (page.length > 0 && bound !== undefined && bytes + size > bound) break;
      page.push(event);
      bytes += size;
    }
    const last = page.at(-1)?.sequence ?? from - 1;
    const total = this.#options.withTotals === true ? {totalEvents: journal.length} : {};
    return {
      kind: 'page',
      events: page,
      next: last + 1,
      complete: page.length === rest.length,
      ...total,
    };
  }
}
