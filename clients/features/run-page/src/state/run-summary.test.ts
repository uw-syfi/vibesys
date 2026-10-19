import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent, RunOverview, Span} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {CandidateFold} from '@vibesys/run-state/candidates';
import {EMPTY_TRACE, foldTrace, traceSpans} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {implementerOutcomes} from './idea-outcomes.js';
import {EMPTY_TRACE_LOG, traceAt, traceStep} from './live.js';
import {type RunPageState, runPageInitial, runPageStep} from './run-page.js';
import {summaryFacts, type WireSummary, type WireTokens} from './run-summary.js';
import {turnTranscript} from './turn-transcript.js';
import {historyLoading, historyProblem, traceArrival, transcriptLoading} from './views.js';

/** The shared fixture: a journal and the summary the helper folds from it (`runs.summary`). */
const FIXTURE = resolve(
  import.meta.dir,
  '../../../../../tests/conformance/fixtures/run-summary/two-ideas.json',
);

interface WireTally {
  readonly input: number | null;
  readonly cache_read: number | null;
  readonly cache_write: number | null;
  readonly output: number | null;
  readonly reasoning: number | null;
}

interface Fixture {
  readonly events: readonly RunEvent[];
  readonly summary: {
    readonly through_sequence: number;
    readonly event_count: number;
    readonly trace: 'recorded' | 'predates_trace';
    readonly rounds_finished: number;
    readonly spans: readonly Span[];
    readonly overview: RunOverview | null;
    readonly replies: readonly {
      readonly span_id: string;
      readonly kind: string;
      readonly json_schema: unknown;
      readonly value: unknown;
    }[];
    readonly tokens: WireTally;
    readonly usage: readonly {readonly span_id: string; readonly tally: WireTally}[];
  };
}

const fixture = JSON.parse(readFileSync(FIXTURE, 'utf8')) as Fixture;

const tokens = (tally: WireTally): WireTokens => ({
  input: tally.input,
  cacheRead: tally.cache_read,
  cacheWrite: tally.cache_write,
  output: tally.output,
  reasoning: tally.reasoning,
});

/** The summary as the host's reader hands it over (`RunSummary` of `@vibesys/ipc`). */
function wire(summary: Fixture['summary']): WireSummary {
  return {
    throughSequence: summary.through_sequence,
    eventCount: summary.event_count,
    trace: summary.trace,
    roundsFinished: summary.rounds_finished,
    journalBytes: 1234,
    overview: summary.overview,
    spans: summary.spans,
    replies: summary.replies.map(reply => ({
      spanId: reply.span_id,
      kind: reply.kind,
      jsonSchema: reply.json_schema,
      value: reply.value,
    })),
    tokens: tokens(summary.tokens),
    usage: summary.usage.map(entry => ({spanId: entry.span_id, tokens: tokens(entry.tally)})),
    records: [],
    inputGates: [],
    terminal: null,
  };
}

const RUN_ID = '20261010-120000-11111111-dynamic';

describe('a run seeded from its summary holds what the whole journal holds for the run level', () => {
  const facts = summaryFacts(wire(fixture.summary), RUN_ID, 'completed');
  const seeded = reduceEventBatch(initialCoreState(), facts.seed);
  const whole = reduceEventBatch(initialCoreState(), fixture.events);

  test('the spend: the run total and each turn span', () => {
    expect(seeded.tokens).toEqual(whole.tokens);
  });

  test('a reported zero stays zero when no turn span names that spend', () => {
    const empty = wire(fixture.summary);
    const summary = {
      ...empty,
      usage: [],
      tokens: {...empty.tokens, input: 0, output: null},
    };
    const zero = reduceEventBatch(
      initialCoreState(),
      summaryFacts(summary, RUN_ID, 'completed').seed,
    );
    expect(zero.tokens.total.input).toBe(0);
    expect(zero.tokens.total.output).toBeNull();
    expect(zero.tokens.bySpan).toEqual({});
  });

  test('the idea rows read each implementer’s reply from the seeded core', () => {
    const candidates = new CandidateFold();
    candidates.apply(fixture.events);
    expect(implementerOutcomes(seeded, candidates.tree())).toEqual(
      implementerOutcomes(whole, candidates.tree()),
    );
  });

  test('the snapshot is the run’s spans and cursor', () => {
    expect(facts.snapshot.spans).toEqual(fixture.summary.spans as never);
    expect(facts.snapshot.sequence).toBe(fixture.summary.through_sequence);
    expect(facts.through).toBe(fixture.summary.through_sequence);
  });

  test('the snapshot carries the registry status requested by the run page', () => {
    expect(summaryFacts(wire(fixture.summary), RUN_ID, 'failed').snapshot.status).toBe('failed');
  });

  test('a summary that is not a snapshot of a run is refused, naming what is wrong', () => {
    const broken = {...wire(fixture.summary), spans: [{id: 7}]};
    expect(() => summaryFacts(broken, RUN_ID, 'completed')).toThrow();
  });
});

test('one span read reconstructs its tool transcript without reading the other 60 run events', () => {
  const events = fixtureNamed('finished-run').events;
  const spanId = 'op:w5-turn';
  const trace = foldTrace(EMPTY_TRACE, events);
  const whole = reduceEventBatch(initialCoreState(), events);
  const selected = events.filter(event => event.span_id === spanId);
  const detail = reduceEventBatch(initialCoreState(), selected);
  expect(selected.length).toBeLessThan(events.length);
  expect(turnTranscript(trace, detail, spanId)).toEqual(turnTranscript(trace, whole, spanId));
});

function initial(): RunPageState {
  return runPageInitial({
    identity: {host: 'ssh:host-b', runId: RUN_ID},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
}

const ENDED = {
  type: 'record',
  record: {
    kind: 'ended',
    run: {
      id: '0123456789ab',
      phase: 'stopped',
      end: {outcome: 'stopped', reason: 'Stopped by you.', resumable: false},
    },
  },
} as never;

describe('an ended run opened from its summary', () => {
  const facts = summaryFacts(wire(fixture.summary), RUN_ID, 'completed');

  function opened() {
    const asked = runPageStep(initial(), ENDED);
    const request = asked.requests.find(each => each.kind === 'read-summary');
    if (request?.kind !== 'read-summary') throw new Error('no summary asked');
    const state = runPageStep(asked.state, {
      type: 'summary',
      token: request.token,
      reply: {kind: 'summary', ...facts},
      wallMs: 0,
    });
    return {asked, state};
  }

  test('asks for the summary first and no history', () => {
    const {asked} = opened();
    const kinds = asked.requests.map(each => each.kind);
    expect(kinds).toContain('read-summary');
    expect(asked.requests.find(each => each.kind === 'read-summary')).toMatchObject({
      status: 'stopped',
    });
    expect(kinds).not.toContain('read-history');
    expect(traceArrival(asked.state)).toBe('awaited');
  });

  test('is final and interactive at once: spans, spend and replies, with no history asked', () => {
    const {state} = opened();
    expect(state.requests).toEqual([]);
    expect(state.state.history).toEqual({kind: 'idle'});
    expect(traceArrival(state.state)).toBe('arrived');
    expect(historyLoading(state.state)).toBeNull();
    expect(traceSpans(state.state.run.trace.head)).toEqual(fixture.summary.spans);
    expect(state.state.run.core?.tokens).toEqual(
      reduceEventBatch(initialCoreState(), fixture.events).tokens,
    );
  });

  test('reads the transcript only when a pane needs it, behind a progress that is the pane’s', () => {
    const {state} = opened();
    const needed = runPageStep(state.state, {type: 'need-history'});
    expect(needed.requests.map(each => each.kind)).toEqual(['read-history']);
    expect(transcriptLoading(needed.state)).not.toBeNull();
    expect(historyLoading(needed.state)).toBeNull();
    expect(traceArrival(needed.state)).toBe('arrived');
    // Asking again while it reads, or once it is read, asks for nothing more.
    expect(runPageStep(needed.state, {type: 'need-history'}).requests).toEqual([]);
  });

  test('reads only an opened step span and keeps the summary unchanged', () => {
    const {state} = opened();
    const spanId = 'op:000001/implementer.1';
    const wanted = runPageStep(state.state, {type: 'need-span', spanId});
    expect(wanted.requests).toMatchObject([{kind: 'read-span', spanId, from: 1, max: 2000}]);
    expect(wanted.state.history).toEqual({kind: 'idle'});
    expect(wanted.state.run).toBe(state.state.run);
    const first = wanted.requests[0];
    if (first?.kind !== 'read-span') throw new Error('no span read');
    const page = runPageStep(wanted.state, {
      type: 'span',
      token: first.token,
      reply: {
        kind: 'page',
        events: [fixture.events[5] as RunEvent],
        next: 7,
        complete: false,
        total: 2,
      },
    });
    expect(page.requests).toMatchObject([{kind: 'read-span', spanId, from: 7}]);
    const last = page.requests[0];
    if (last?.kind !== 'read-span') throw new Error('no next page');
    const complete = runPageStep(page.state, {
      type: 'span',
      token: last.token,
      reply: {
        kind: 'page',
        events: [fixture.events[18] as RunEvent],
        next: 20,
        complete: true,
        total: 2,
      },
    });
    expect(complete.state.detail).toMatchObject({kind: 'ready', spanId});
    expect(complete.state.history).toEqual({kind: 'idle'});
    expect(complete.state.run).toBe(state.state.run);
  });

  test('an older helper falls back to the full history when a span is opened', () => {
    const {state} = opened();
    const needed = runPageStep(state.state, {type: 'need-span', spanId: 'op:turn'});
    const request = needed.requests[0];
    if (request?.kind !== 'read-span') throw new Error('no span read');
    const unsupported = runPageStep(needed.state, {
      type: 'span',
      token: request.token,
      reply: {kind: 'unsupported'},
    });
    expect(unsupported.requests.map(each => each.kind)).toContain('read-history');
    expect(unsupported.state.history.kind).toBe('reading');
    expect(unsupported.state.run).toBe(state.state.run);
  });

  test('a late page for a previously opened step cannot replace the selected detail', () => {
    const {state} = opened();
    const first = runPageStep(state.state, {type: 'need-span', spanId: 'op:first'});
    const firstRequest = first.requests[0];
    if (firstRequest?.kind !== 'read-span') throw new Error('no first span read');
    const second = runPageStep(first.state, {type: 'need-span', spanId: 'op:second'});
    const late = runPageStep(second.state, {
      type: 'span',
      token: firstRequest.token,
      reply: {kind: 'page', events: [], next: 1, complete: true, total: 0},
    });
    expect(late.state).toBe(second.state);
    expect(late.state.detail).toMatchObject({kind: 'reading', spanId: 'op:second'});
  });

  test('remembers a transcript request made before the summary arrives', () => {
    const opened = runPageStep(initial(), ENDED);
    const request = opened.requests.find(each => each.kind === 'read-summary');
    if (request?.kind !== 'read-summary') throw new Error('no summary asked');
    const wanted = runPageStep(opened.state, {type: 'need-history'});
    expect(wanted.requests).toEqual([]);
    const ready = runPageStep(wanted.state, {
      type: 'summary',
      token: request.token,
      reply: {kind: 'summary', ...facts},
      wallMs: 0,
    });
    expect(ready.requests.map(each => each.kind)).toEqual(['read-history']);
    expect(transcriptLoading(ready.state)).not.toBeNull();
  });

  test('a transcript that cannot be read leaves the run level standing', () => {
    const {state} = opened();
    const needed = runPageStep(state.state, {type: 'need-history'});
    const request = needed.requests[0];
    if (request?.kind !== 'read-history') throw new Error('no history asked');
    const failed = runPageStep(needed.state, {
      type: 'history',
      token: request.token,
      reply: {kind: 'failed', message: 'channel closed'},
    });
    expect(failed.state.run).toBe(needed.state.run);
    expect(traceSpans(failed.state.run.trace.head)).toEqual(fixture.summary.spans);
    expect(traceArrival(failed.state)).toBe('arrived');
  });

  test('a host without history keeps a ready summary visible', () => {
    const {state} = opened();
    const needed = runPageStep(state.state, {type: 'need-history'});
    const request = needed.requests[0];
    if (request?.kind !== 'read-history') throw new Error('no history asked');
    const unsupported = runPageStep(needed.state, {
      type: 'history',
      token: request.token,
      reply: {kind: 'unsupported'},
    });
    expect(traceArrival(unsupported.state)).toBe('arrived');
    expect(traceSpans(unsupported.state.run.trace.head)).toEqual(fixture.summary.spans);
  });

  test('a host with no summary is read through its history, as before', () => {
    const asked = runPageStep(initial(), ENDED);
    const request = asked.requests.find(each => each.kind === 'read-summary');
    if (request?.kind !== 'read-summary') throw new Error('no summary asked');
    const older = runPageStep(asked.state, {
      type: 'summary',
      token: request.token,
      reply: {kind: 'unsupported'},
      wallMs: 0,
    });
    expect(older.requests.map(each => each.kind)).toEqual(['read-history']);
  });

  test('a summary that fails falls back to full history, then shows the run', () => {
    const asked = runPageStep(initial(), ENDED);
    const request = asked.requests.find(each => each.kind === 'read-summary');
    if (request?.kind !== 'read-summary') throw new Error('no summary asked');
    const fallback = runPageStep(asked.state, {
      type: 'summary',
      token: request.token,
      reply: {kind: 'failed', message: 'channel closed'},
      wallMs: 0,
    });
    expect(fallback.requests.map(each => each.kind)).toEqual(['read-history']);
    expect(historyLoading(fallback.state)).not.toBeNull();
    expect(historyProblem(fallback.state)).toBeNull();
    const history = fallback.requests[0];
    if (history?.kind !== 'read-history') throw new Error('no history asked');
    const loaded = runPageStep(fallback.state, {
      type: 'history',
      token: history.token,
      reply: {
        kind: 'page',
        events: fixture.events,
        next: (fixture.events.at(-1)?.sequence ?? 0) + 1,
        complete: true,
      },
    });
    expect(traceArrival(loaded.state)).toBe('arrived');
    expect(historyProblem(loaded.state)).toBeNull();
    expect(traceSpans(loaded.state.run.trace.head)).toEqual(fixture.summary.spans);
  });

  test('only a failed summary and failed history show an error', () => {
    const asked = runPageStep(initial(), ENDED);
    const request = asked.requests.find(each => each.kind === 'read-summary');
    if (request?.kind !== 'read-summary') throw new Error('no summary asked');
    const fallback = runPageStep(asked.state, {
      type: 'summary',
      token: request.token,
      reply: {kind: 'failed', message: 'summary closed'},
      wallMs: 0,
    });
    const history = fallback.requests[0];
    if (history?.kind !== 'read-history') throw new Error('no history asked');
    const failed = runPageStep(fallback.state, {
      type: 'history',
      token: history.token,
      reply: {kind: 'failed', message: 'history closed'},
    });
    expect(traceArrival(failed.state)).toBe('none');
    expect(historyProblem(failed.state)).toContain('history closed');
  });

  test('a late or repeated summary reply is dropped by its token', () => {
    const {state} = opened();
    const again = runPageStep(state.state, {
      type: 'summary',
      token: 1,
      reply: {kind: 'summary', ...facts},
      wallMs: 0,
    });
    expect(again.state).toBe(state.state);
  });
});

/** A journal of span events: spans start, update and end, in sequence order with the odd skip. */
function spanEvents(random: Seeded, count: number): RunEvent[] {
  const ids = ['a', 'b', 'c', 'd', 'e'];
  const events: RunEvent[] = [];
  let sequence = 0;
  for (let index = 0; index < count; index += 1) {
    sequence += random.int(7) === 0 ? 2 : 1;
    const id = random.pick(ids);
    const kind = random.pick(['span_started', 'span_updated', 'span_ended']);
    events.push({
      protocol_version: 1,
      sequence,
      type: kind,
      timestamp: '2026-10-15T16:30:51Z',
      data: {
        kind,
        span: {
          id,
          parent: null,
          type: 'flow',
          name: id,
          status: kind === 'span_ended' ? 'done' : 'running',
          started_at: index,
        },
      },
    } as unknown as RunEvent);
  }
  return events;
}

describe('the summary and the live tail meet with no gap and no duplicate', () => {
  test('for any cut, tail batching and overlap, the run is the whole journal’s fold', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const journal = spanEvents(random, random.int(60));
      const whole = foldTrace(EMPTY_TRACE, journal);
      const cut = random.int(journal.length + 1);
      const prefix = foldTrace(EMPTY_TRACE, journal.slice(0, cut));
      const through = journal.slice(0, cut).at(-1)?.sequence ?? 0;

      let log = traceStep(EMPTY_TRACE_LOG, {
        kind: 'snapshot',
        snapshot: {
          run_id: RUN_ID,
          sequence: through,
          status: 'running',
          spans: [...traceSpans(prefix)],
        },
      });
      // The tail arrives in batches that may start early (a replay from before the summary's
      // cursor) and may repeat events a batch before.
      for (let from = cut; from < journal.length; ) {
        const early = Math.max(0, from - random.int(4));
        const to = Math.min(journal.length, from + 1 + random.int(5));
        log = traceStep(log, {kind: 'events', events: journal.slice(early, to)});
        from = to;
      }
      expect(traceSpans(traceAt(log, null)), `seed ${seed}`).toEqual(traceSpans(whole));
      expect(traceAt(log, null).sequence, `seed ${seed}`).toBe(whole.sequence);
    }
  });
});
