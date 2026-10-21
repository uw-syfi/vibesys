/**
 * The main process's store of ended runs' recorded events: one cache per (host, project, run),
 * shared by every window.
 *
 * ## Why it exists
 *
 * Each window's renderer used to read an ended run's whole journal from the host (`runs.read_events`,
 * page by page over ssh) and fold it. A second window, or reopening the run, paid the transfer
 * again. The main process owns the host connection, so it owns the one copy: a window asks the
 * store for a page, and the store answers from memory when any window already read it, or reads
 * it from the host once for all windows that ask while it is in flight.
 *
 * ## What it holds, and what it does not
 *
 * Raw typed pages exactly as the host answered them (`HistoryRead`), keyed by the request that
 * produced them (`from`, `max` and `kinds`), never view models and never folds: each window folds with the
 * same pure `@vibesys/run-state` code. Windows run the same code, so they ask the same pages
 * and hit each other's entries; a window that asks differently just misses and reads the host.
 * Only an ended run's history is immutable, so only it belongs here: callers read a run's history
 * through the store only after its record says it ended, and tell the store when a run is live
 * again (`invalidateRun`, `runsLive`) or its host goes away (`invalidateHost`).
 *
 * ## Bounds
 *
 * The store never holds more than `maxBytes` of pages (`DEFAULT_MAX_BYTES`): when a read would
 * pass it, the least recently read runs are dropped whole until it fits, and a run whose pages
 * alone pass it is not kept at all (every read of it goes to the host). A page's size is its
 * events' serialized length, measured once when it is stored. Failures and `unsupported` replies
 * are never kept: the next read asks the host again.
 *
 * ## Concurrency
 *
 * Reads of one page in flight share one host request. A reply that arrives after its run was
 * invalidated is handed to the readers that asked but is not stored, so a stale page never
 * outlives the change that invalidated it.
 */

/** One run's identity in the store: the host's key, the project path on it, and the run id. */
export interface HistoryRunKey {
  readonly host: string;
  readonly project: string;
  readonly runId: string;
}

/** One page of a run's recorded events as the host answered it (`runs.read_events`). */
export interface HistoryChunk {
  readonly events: readonly unknown[];
  /** The sequence the next page starts at. */
  readonly next: number;
  /** The page reached the end of what the journal holds. */
  readonly complete: boolean;
  /** The journal's events, when the host says (`run_history_pages`). */
  readonly totalEvents?: number;
  /** The journal's bytes, when the host says (`run_history_pages`). */
  readonly totalBytes?: number;
}

/**
 * The events a read keeps, when it keeps only some: the host's `runs.read_events` `kinds`. `trace`
 * is the events the run trace folds, a small part of a journal. A read without kinds holds
 * every event; the two never share a held page.
 */
export type HistoryKinds = 'trace';

/** A page, or that the host's VibeSys does not serve history. */
export type HistoryRead = ({readonly kind: 'page'} & HistoryChunk) | {readonly kind: 'unsupported'};

/** Where pages come from when the store has none: the host, read through its connection. */
export interface HistorySource {
  read(run: HistoryRunKey, from: number, max: number, kinds?: HistoryKinds): Promise<HistoryRead>;
}

/** What the store is built from. */
export interface RunHistoryOptions {
  readonly source: HistorySource;
  /** The most bytes of pages held across all runs; `DEFAULT_MAX_BYTES` when absent. */
  readonly maxBytes?: number;
}

/** The default bound on held pages: 256 MiB, room for about thirty runs of the size measured (8 MB). */
export const DEFAULT_MAX_BYTES = 256 * 1024 * 1024;

/** What the store has done, for the app's log and for tests. */
export interface RunHistoryStats {
  /** Runs holding at least one page. */
  readonly runs: number;
  /** Pages held. */
  readonly pages: number;
  /** Bytes of pages held, as measured when each was stored. */
  readonly bytes: number;
  /** Reads answered from a held page. */
  readonly hits: number;
  /** Reads that went to the source, or joined a read already in flight. */
  readonly misses: number;
  /** Runs dropped to stay within `maxBytes`. */
  readonly evictions: number;
}

interface HeldPage {
  readonly read: HistoryRead & {readonly kind: 'page'};
  readonly bytes: number;
}

interface HeldRun {
  readonly pages: Map<string, HeldPage>;
  bytes: number;
}

const SEPARATOR = '\u0000';

function runId(run: HistoryRunKey): string {
  return [run.host, run.runId, run.project].join(SEPARATOR);
}

function pageId(from: number, max: number, kinds: HistoryKinds | undefined): string {
  return `${from}:${max}:${kinds ?? ''}`;
}

/** A page's size: the length of its events' JSON, which is the bytes they were read as. */
function sizeOf(read: HistoryRead & {readonly kind: 'page'}): number {
  return JSON.stringify(read.events).length;
}

export class RunHistoryStore {
  readonly #source: HistorySource;
  readonly #maxBytes: number;
  /** Held runs, least recently read first (a Map keeps insertion order; reads move a run last). */
  readonly #runs = new Map<string, HeldRun>();
  /**
   * How many times each run was invalidated, kept after eviction so a read that began before an
   * invalidation cannot store its reply after it.
   */
  readonly #generations = new Map<string, number>();
  readonly #flights = new Map<string, Promise<HistoryRead>>();
  #bytes = 0;
  #hits = 0;
  #misses = 0;
  #evictions = 0;

  constructor(options: RunHistoryOptions) {
    this.#source = options.source;
    this.#maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;
  }

  /**
   * The page of `run`'s history of up to `max` events from sequence `from` (only the events of
   * `kinds`, when given): a held page, else the host's (read once for every caller that asks while
   * it is in flight, then held). Rejects with the source's failure, which is not held.
   */
  read(run: HistoryRunKey, from: number, max: number, kinds?: HistoryKinds): Promise<HistoryRead> {
    const id = runId(run);
    const page = pageId(from, max, kinds);
    const held = this.#runs.get(id)?.pages.get(page);
    if (held !== undefined) {
      this.#hits += 1;
      this.#touch(id);
      return Promise.resolve(held.read);
    }
    this.#misses += 1;
    const flightId = `${id}${SEPARATOR}${page}`;
    const flying = this.#flights.get(flightId);
    if (flying !== undefined) return flying;
    const generation = this.#generations.get(id) ?? 0;
    const flight = this.#source.read(run, from, max, kinds).then(
      read => {
        this.#flights.delete(flightId);
        if (read.kind === 'page' && (this.#generations.get(id) ?? 0) === generation) {
          this.#hold(id, page, read);
        }
        return read;
      },
      (error: unknown) => {
        this.#flights.delete(flightId);
        throw error;
      },
    );
    this.#flights.set(flightId, flight);
    return flight;
  }

  /**
   * Run `runId` of `host` is live again (a resume) or gone: drop what is held for it, under
   * whatever project path a window named, and refuse what is in flight.
   */
  invalidateRun(host: string, runId: string): void {
    this.#invalidateWhere(`${host}${SEPARATOR}${runId}${SEPARATOR}`);
  }

  /** Every run of `host` is no longer known to be ended (the host was removed or its registry changed). */
  invalidateHost(host: string): void {
    this.#invalidateWhere(`${host}${SEPARATOR}`);
  }

  /**
   * The host's registry lists these runs as live: each is no longer an ended run, so whatever is
   * held for it is dropped (a resumed run appends to the journal it had).
   */
  runsLive(host: string, runIds: Iterable<string>): void {
    for (const id of runIds) this.invalidateRun(host, id);
  }

  stats(): RunHistoryStats {
    let pages = 0;
    for (const run of this.#runs.values()) pages += run.pages.size;
    return {
      runs: this.#runs.size,
      pages,
      bytes: this.#bytes,
      hits: this.#hits,
      misses: this.#misses,
      evictions: this.#evictions,
    };
  }

  /** Invalidate every run whose id starts with `prefix`: held, or only remembered by generation. */
  #invalidateWhere(prefix: string): void {
    const keys = new Set([...this.#runs.keys(), ...this.#generations.keys(), ...this.#inFlight()]);
    for (const id of keys) {
      if (!id.startsWith(prefix)) continue;
      this.#generations.set(id, (this.#generations.get(id) ?? 0) + 1);
      this.#drop(id);
    }
  }

  /** The runs a read is in flight for. */
  #inFlight(): string[] {
    return [...this.#flights.keys()].map(flight => flight.slice(0, flight.lastIndexOf(SEPARATOR)));
  }

  #drop(id: string): void {
    const run = this.#runs.get(id);
    if (run === undefined) return;
    this.#bytes -= run.bytes;
    this.#runs.delete(id);
  }

  #touch(id: string): void {
    const run = this.#runs.get(id);
    if (run === undefined) return;
    this.#runs.delete(id);
    this.#runs.set(id, run);
  }

  #hold(id: string, page: string, read: HistoryRead & {readonly kind: 'page'}): void {
    const bytes = sizeOf(read);
    if (bytes > this.#maxBytes) return;
    const run = this.#runs.get(id) ?? {pages: new Map<string, HeldPage>(), bytes: 0};
    const replaced = run.pages.get(page);
    if (replaced !== undefined) {
      run.bytes -= replaced.bytes;
      this.#bytes -= replaced.bytes;
    }
    run.pages.set(page, {read, bytes});
    run.bytes += bytes;
    this.#bytes += bytes;
    this.#runs.delete(id);
    this.#runs.set(id, run);
    this.#fit(id);
  }

  /** Drop the least recently read runs until the pages fit; `keep` goes last, and whole. */
  #fit(keep: string): void {
    for (const id of [...this.#runs.keys()]) {
      if (this.#bytes <= this.#maxBytes) return;
      if (id === keep) continue;
      this.#drop(id);
      this.#evictions += 1;
    }
    if (this.#bytes > this.#maxBytes) {
      this.#drop(keep);
      this.#evictions += 1;
    }
  }
}
