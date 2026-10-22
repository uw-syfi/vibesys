import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {RUN_EVENT_TYPES_BY_KINDS} from '@vibesys/contracts/host-helper';
import type {RunHistoryPage, WelcomeResult} from '@vibesys/ipc';
import {Seeded} from '@vibesys/store/testing';
import {
  ENDED_RECORD,
  type Journal,
  journalOf,
  summaryReplyOf,
  traceSequences,
} from '../../state/ended-journal-fixtures.js';
import {traceAt} from '../../state/live.js';
import {RUN} from '../../state/live-run-fixtures.js';
import {scrubberView, viewAtPosition} from '../../state/scrub.js';
import {createRunPageStore, type RunPagePorts} from './store.js';
import {TracePageCache, traceReader} from './trace-pages.js';

/**
 * The Run screen's store over a Fake host that serves an ended run's journal in pages: what the
 * scrubber can stand at, what a view at an earlier position reads, and which pages were asked.
 */

/** Lets the promises the store waits on settle: microtasks only, no time. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 200; turn += 1) await Promise.resolve();
}

/**
 * What a host's read of the journal returns: every event (`all`, a host without the trace kind), only
 * the trace's events in pages of `size` of them, the cursor after the last one (`selected`, a helper
 * with the kind), or only the trace's events of each `size`-sequence window of the journal, so a
 * page may hold few or none and not be complete (`windowed`, which the reader must also accept).
 */
type Serves = 'all' | 'selected' | 'windowed';

const TRACE_TYPES: ReadonlySet<string> = new Set(RUN_EVENT_TYPES_BY_KINDS.trace);

/** A host serving `journal` as `runs.read_events` does, `size` events a page; it can hold its answers. */
class FakeJournalHost {
  readonly asked: {readonly from: number; readonly max: number}[] = [];
  inFlight = 0;
  mostInFlight = 0;
  #gate: Promise<void> | null = null;
  #open: (() => void) | null = null;

  constructor(
    readonly journal: Journal,
    readonly size: number,
    readonly serves: Serves = 'all',
  ) {}

  /** Hold the answers until `release`. */
  hold(): void {
    this.#gate = new Promise(resolve => {
      this.#open = resolve;
    });
  }

  release(): void {
    this.#open?.();
    this.#gate = null;
  }

  async read(from: number, max: number): Promise<WelcomeResult<RunHistoryPage>> {
    this.asked.push({from, max});
    this.inFlight += 1;
    this.mostInFlight = Math.max(this.mostInFlight, this.inFlight);
    await this.#gate;
    this.inFlight -= 1;
    return {ok: true, value: this.#page(from, max)};
  }

  #page(from: number, max: number): RunHistoryPage {
    const {events, through} = this.journal;
    const at = (event: RunEvent): number => event.sequence ?? 0;
    if (this.serves === 'selected') {
      const rest = events.filter(event => TRACE_TYPES.has(event.type) && at(event) >= from);
      const page = rest.slice(0, Math.min(max, this.size));
      const complete = page.length === rest.length;
      const last = page.at(-1);
      const next = complete || last === undefined ? Math.max(from, through + 1) : at(last) + 1;
      return {kind: 'page', events: page, next, complete};
    }
    const next = Math.min(from + Math.min(max, this.size), through + 1);
    const window = events.filter(event => at(event) >= from && at(event) < next);
    return {
      kind: 'page',
      events: this.serves === 'all' ? window : window.filter(event => TRACE_TYPES.has(event.type)),
      next,
      complete: next > through,
    };
  }
}

function portsOver(host: FakeJournalHost, cache: TracePageCache): RunPagePorts {
  const ignore = (): void => {};
  const unsupported = () => Promise.resolve({kind: 'unsupported'} as const);
  return {
    attach: () => Promise.resolve(null),
    release: ignore,
    attachReader: () => Promise.resolve({kind: 'attached'}),
    openSession: () => ({
      chat: {
        ask: () => Promise.resolve({ok: true}),
        createThread: () => Promise.resolve({ok: true, threadId: 't'}),
        control: () => Promise.resolve({kind: 'acknowledged'}),
      },
      command: () => Promise.resolve(null),
      close: ignore,
    }),
    readHistory: unsupported,
    readTrace: traceReader(
      () => `${RUN.host} ${RUN.runId}`,
      cache,
      (from, max) => host.read(from, max),
    ),
    readSummary: () => Promise.resolve(summaryReplyOf(host.journal)),
    readSpan: unsupported,
    resumeEnded: ignore,
    openWindow: ignore,
    now: () => 0,
    archive: ignore,
    runAgain: ignore,
    logs: null,
  };
}

const NEVER = (): (() => void) => () => {};

function open(host: FakeJournalHost, cache: TracePageCache) {
  const store = createRunPageStore(
    {
      identity: RUN,
      title: {project: '~/project', task: 'serve'},
      hostLabel: 'host-a',
      unavailable: {},
    },
    portsOver(host, cache),
    NEVER,
  );
  store.dispatch({type: 'record', record: ENDED_RECORD});
  return store;
}

describe('the store of an ended run opened from its summary', () => {
  const journal = journalOf(new Seeded(21), 120);

  test('reads the history one page at a time and the scrubber reaches every span event', async () => {
    const host = new FakeJournalHost(journal, 17);
    const store = open(host, new TracePageCache());
    await settle();
    const log = store.getState().run.trace;
    expect(host.mostInFlight).toBe(1);
    expect(host.asked.map(each => each.from)).toEqual(
      Array.from({length: Math.ceil(journal.through / 17)}, (_, index) => 1 + index * 17).filter(
        from => from < journal.through,
      ),
    );
    const bar = scrubberView(log, {at: 'live'}, true);
    const older = traceSequences(journal).filter(at => at < journal.through);
    expect(bar?.last).toBe(older.length + 1);
    // A view at an earlier position reads the trace as of it, and the end follows the run.
    const early = viewAtPosition(log, 1);
    if (early.at === 'live') throw new Error('the first position is not the end');
    expect(traceAt(log, early.at)).not.toBe(log.head);
    expect(viewAtPosition(log, bar?.last ?? 0)).toEqual({at: 'live'});
    store.dispose();
  });

  test('leaving the run stops the read', async () => {
    const host = new FakeJournalHost(journal, 17);
    host.hold();
    const store = open(host, new TracePageCache());
    await settle();
    expect(host.asked).toHaveLength(1);
    const before = store.getState().run.trace;
    store.dispose();
    host.release();
    await settle();
    expect(host.asked).toHaveLength(1);
    expect(store.getState().run.trace).toBe(before);
  });

  test('opening the run again reads nothing from the host', async () => {
    const host = new FakeJournalHost(journal, 17);
    const cache = new TracePageCache();
    const first = open(host, cache);
    await settle();
    const asked = host.asked.length;
    const positions = [...first.getState().run.trace.positions];
    first.dispose();
    const second = open(host, cache);
    await settle();
    expect(host.asked).toHaveLength(asked);
    expect([...second.getState().run.trace.positions]).toEqual(positions);
    second.dispose();
  });

  test('a host that cannot serve history leaves the scrubber at its one position, without a notice', async () => {
    const host = new FakeJournalHost(journal, 17);
    const ports = {
      ...portsOver(host, new TracePageCache()),
      readTrace: () => Promise.resolve({kind: 'unsupported'} as const),
    };
    const store = createRunPageStore(
      {
        identity: RUN,
        title: {project: '~/project', task: null},
        hostLabel: 'host-a',
        unavailable: {},
      },
      ports,
      NEVER,
    );
    store.dispatch({type: 'record', record: ENDED_RECORD});
    await settle();
    const state = store.getState();
    expect(scrubberView(state.run.trace, {at: 'live'}, true)?.last).toBe(0);
    expect(state.notice).toBeNull();
    expect(state.history.kind).toBe('idle');
    store.dispose();
  });

  test('a read that throws keeps the positions already read', async () => {
    const host = new FakeJournalHost(journal, 17);
    let reads = 0;
    const ports = {
      ...portsOver(host, new TracePageCache()),
      readTrace: (from: number, max: number) => {
        reads += 1;
        return reads > 2
          ? Promise.reject(new Error('channel closed'))
          : traceReader(
              () => 'run',
              new TracePageCache(),
              (f, m) => host.read(f, m),
            )(from, max);
      },
    };
    const store = createRunPageStore(
      {
        identity: RUN,
        title: {project: '~/project', task: null},
        hostLabel: 'host-a',
        unavailable: {},
      },
      ports,
      NEVER,
    );
    store.dispatch({type: 'record', record: ENDED_RECORD});
    await settle();
    expect(reads).toBe(3);
    const bar = scrubberView(store.getState().run.trace, {at: 'live'}, true);
    expect(bar?.last).toBeGreaterThan(0);
    expect(store.getState().notice).toBeNull();
    store.dispose();
  });
});

describe('the scrubber of an ended run from a host that selects the trace events', () => {
  const sizes = [1, 3, 17, 64, 500];

  /** The positions the scrubber can stand at after the whole read, and how often the host was asked. */
  async function read(journal: Journal, size: number, serves: Serves) {
    const host = new FakeJournalHost(journal, size, serves);
    const store = open(host, new TracePageCache());
    // One event a page takes more turns than `settle` gives: wait for the read to end instead.
    for (let turn = 0; turn < 100_000 && store.getState().traceHistory.kind !== 'done'; turn += 1) {
      await Promise.resolve();
    }
    const state = store.getState();
    const result = {
      positions: [...state.run.trace.positions],
      last: scrubberView(state.run.trace, {at: 'live'}, true)?.last,
      asked: host.asked.length,
      reading: state.history.kind,
    };
    store.dispose();
    return result;
  }

  test('reaches the positions it reaches from every event, for any journal and page size', async () => {
    for (const seed of [1, 2, 3, 5, 8, 13, 21]) {
      const journal = journalOf(new Seeded(seed), 5 + seed * 17);
      for (const size of sizes) {
        const whole = await read(journal, size, 'all');
        expect(whole.positions.length).toBeGreaterThan(0);
        for (const serves of ['selected', 'windowed'] as const) {
          const selected = await read(journal, size, serves);
          expect({serves, size, ...selected, asked: 0}).toEqual({
            serves,
            size,
            ...whole,
            asked: 0,
          });
          expect(selected.asked).toBeLessThanOrEqual(whole.asked);
        }
      }
    }
  });

  test('asks for fewer pages when the journal is mostly other events', async () => {
    const journal = journalOf(new Seeded(34), 400);
    const whole = await read(journal, 10, 'all');
    const selected = await read(journal, 10, 'selected');
    expect(selected.positions).toEqual(whole.positions);
    expect(selected.asked).toBeLessThan(whole.asked);
  });

  test('keeps going over pages that hold nothing and are not the end', async () => {
    const journal = journalOf(new Seeded(55), 300);
    const host = new FakeJournalHost(journal, 4, 'windowed');
    const store = open(host, new TracePageCache());
    for (let turn = 0; turn < 100_000 && store.getState().traceHistory.kind !== 'done'; turn += 1) {
      await Promise.resolve();
    }
    expect(host.asked.length).toBe(Math.ceil((journal.through - 1) / 4));
    const older = traceSequences(journal).filter(at => at < journal.through);
    expect(scrubberView(store.getState().run.trace, {at: 'live'}, true)?.last).toBe(
      older.length + 1,
    );
    store.dispose();
  });
});
