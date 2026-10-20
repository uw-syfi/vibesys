import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import type {HostRun} from '@vibesys/hosts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {EMPTY_TRACE, foldTrace, traceSpans} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {outcomeWords} from './idea-words.js';
import {ageWords, IDEAS_FILTER_DEFAULT, ideasView} from './ideas-view.js';
import {failuresAt} from './input-gates.js';
import {clockAt, clockStart, heroView, nowView} from './run-level.js';
import {createRunReader, type RunModel} from './run-model.js';
import {
  type RunPageRequest,
  type RunPageState,
  type RunRecord,
  runPageInitial,
  runPageStep,
} from './run-page.js';
import {
  type SummaryReply,
  summaryFacts,
  type WireGate,
  type WireSummary,
  type WireTerminal,
} from './run-summary.js';
import {endedView, runEnding} from './views.js';

/**
 * The run page on a run that ended badly: a real run that crashed (its host Git refused the
 * workspace) 29 seconds after its implementer turn finished, leaving the idea, the turn and the
 * round it was in open, with a baseline whose gates failed. The fixture is that run's journal.
 */
const fixture = fixtureNamed('crashed-after-turn');
const RUN = {host: 'ssh:host-a', runId: fixture.runId} as const;
const FULL_REASON = fixture.end?.reason ?? '';
const CLIPPED = `${FULL_REASON.slice(0, 199)}…`;

function ended(status: HostRun['phase'], extra: Partial<HostRun> = {}): RunRecord {
  const run: HostRun = {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/project',
    runId: fixture.runId,
    status,
    phase: status,
    paused: null,
    end: {outcome: 'failed', reason: CLIPPED, resumable: true, resume_from_round: 1},
    startedAt: 1,
    reason: FULL_REASON,
    ...extra,
  };
  return {kind: 'ended', run};
}

function opened(record: RunRecord): {state: RunPageState; requests: readonly RunPageRequest[]} {
  const state = runPageInitial({
    identity: RUN,
    title: {project: '~/project', task: 'serve'},
    hostLabel: 'host-a',
    unavailable: {},
  });
  return runPageStep(state, {type: 'record', record});
}

function summaryToken(requests: readonly RunPageRequest[]): number {
  const read = requests.find(request => request.kind === 'read-summary');
  if (read?.kind !== 'read-summary') throw new Error('the page did not ask for the summary');
  return read.token;
}

/** An ended run opened as a host that serves no summary does: its whole history, read in pages. */
function recordRun(): HostRun {
  const record = ended('failed');
  if (record.kind !== 'ended') throw new Error('not ended');
  return record.run;
}

function fold(record: RunRecord, batches: readonly (readonly RunEvent[])[]): RunPageState {
  const first = opened(record);
  let step = runPageStep(first.state, {
    type: 'summary',
    token: summaryToken(first.requests),
    reply: {kind: 'unsupported'},
    wallMs: 0,
  });
  let from = 1;
  for (const [at, events] of batches.entries()) {
    const read = step.requests.find(request => request.kind === 'read-history');
    if (read?.kind !== 'read-history') throw new Error('the page did not ask for a page');
    const next = (events.at(-1)?.sequence ?? from) + 1;
    from = next;
    step = runPageStep(step.state, {
      type: 'history',
      token: read.token,
      reply: {kind: 'page', events, next, complete: at === batches.length - 1},
    });
  }
  return step.state;
}

/** The gates that finished before the first idea began, as a host words them. */
function inputGatesOf(events: readonly RunEvent[]): WireGate[] {
  const isIdea = (event: RunEvent): boolean => {
    const data = event.data;
    return (
      event.type === 'span_started' &&
      data !== null &&
      data !== undefined &&
      'span' in data &&
      data.span.label?.kind === 'workstream'
    );
  };
  const idea = events.findIndex(isIdea);
  const gates: WireGate[] = [];
  for (const event of idea === -1 ? events : events.slice(0, idea)) {
    const data = event.data;
    if (data?.kind !== 'gate_finished') continue;
    gates.push({
      sequence: event.sequence ?? 0,
      gate: data.gate,
      recipe: data.recipe ?? null,
      passed: event.status !== 'failed',
      outputTail: data.output_tail ?? null,
    });
  }
  return gates;
}

/** The event that ended the fixture's journal, as a host words it. */
function terminalOf(events: readonly RunEvent[]): WireTerminal | null {
  const failed = events.filter(event => event.type === 'run_failed').at(-1);
  return failed === undefined
    ? null
    : {sequence: failed.sequence ?? 0, kind: 'failed', message: failed.text || null};
}

/**
 * The summary a host would send for the fixture's journal: the run's spans as its fold left them,
 * its usage records, the gates that finished before the first idea, and the event that ended it.
 */
function wireSummaryOf(events: readonly RunEvent[]): WireSummary {
  const none = {input: null, cacheRead: null, cacheWrite: null, output: null, reasoning: null};
  return {
    throughSequence: events.at(-1)?.sequence ?? 0,
    eventCount: events.length,
    journalBytes: 0,
    trace: 'recorded',
    roundsFinished: 0,
    overview: null,
    spans: [...traceSpans(foldTrace(EMPTY_TRACE, events))],
    replies: [],
    tokens: none,
    usage: [],
    records: reduceEventBatch(initialCoreState(), events).usageRecords,
    inputGates: inputGatesOf(events),
    terminal: terminalOf(events),
  };
}

/** An ended run opened from its summary, as a remote run is: the host's summary and its registry record. */
function summarized(record: RunRecord, events: readonly RunEvent[]): RunPageState {
  const first = opened(record);
  const reply: SummaryReply = {
    kind: 'summary',
    ...summaryFacts(wireSummaryOf(events), fixture.runId, 'failed'),
  };
  return runPageStep(first.state, {
    type: 'summary',
    token: summaryToken(first.requests),
    reply,
    wallMs: 0,
  }).state;
}

function modelOf(state: RunPageState): RunModel {
  return createRunReader().read(state.run.trace, null, runEnding(state));
}

function inBatches(events: readonly RunEvent[], random: Seeded): RunEvent[][] {
  const batches: RunEvent[][] = [];
  let at = 0;
  while (at < events.length) {
    const size = 1 + random.int(40);
    batches.push(events.slice(at, at + size));
    at += size;
  }
  return batches;
}

describe('a run that crashed with work open', () => {
  const state = fold(ended('failed'), [fixture.events]);
  const model = modelOf(state);

  test('no idea, step or agent reads as running', () => {
    expect(model.candidates.candidates.map(idea => idea.outcome)).toEqual(['failed']);
    expect(model.agents.agents.every(agent => agent.status !== 'running')).toBe(true);
    expect(nowView(model)).toEqual([]);
    const view = ideasView(model, IDEAS_FILTER_DEFAULT);
    expect(view.counts).toEqual({running: 0, passed: 0, failed: 1});
    expect(view.rows.map(row => row.state.label)).toEqual(['Failed · run failed']);
  });

  test('the idea says why it ended in words, not Running', () => {
    const [idea] = model.candidates.candidates;
    expect(idea === undefined ? '' : outcomeWords(idea)).toBe('failed · run failed');
  });

  test('a turn that finished before the crash took as long as it recorded, and is done', () => {
    const implementer = model.agents.agents.find(agent => agent.role === 'implementer');
    expect(implementer?.status).toBe('done');
    const took = (implementer?.activity.endedAt ?? 0) - (implementer?.startedAt ?? 0);
    expect(took).toBeCloseTo(1720.893, 2);
  });

  test('the idea lasted until the run was last active, and the clock stops there', () => {
    const [idea] = model.candidates.candidates;
    const length = (idea?.endedAt ?? 0) - (idea?.startedAt ?? 0);
    expect(length).toBeGreaterThan(30 * 60);
    expect(length).toBeLessThan(31 * 60);
    expect(model.latest).toBe(idea?.endedAt ?? Number.NaN);
    const now = clockAt(model, null) ?? 0;
    // The axis ends with the run (about 35 minutes after its first span), not a few minutes in.
    expect((now - clockStart(model)) / 60).toBeGreaterThan(34);
    expect(ageWords(idea?.startedAt ?? 0, now)).toMatch(/^3\d/);
  });

  test('the baseline gate failures are held, both of them', () => {
    expect(model.inputGates.map(failure => failure.gate)).toEqual(['accuracy', 'benchmark']);
  });

  test('the metric tile says the input was not measured, not that nothing was', () => {
    const [tile] = heroView(model).tiles;
    expect(tile?.detail).toBe('input not measured');
  });

  test('the banner gives the whole reason, not the registry’s cut of it', () => {
    expect(CLIPPED.endsWith('…')).toBe(true);
    expect(endedView(state)?.reason).toBe(FULL_REASON);
  });

  test('a run still going is not closed', () => {
    const live: RunPageState = {
      ...state,
      record: {kind: 'live', run: {...recordRun(), end: null, phase: 'running'}},
    };
    expect(runEnding(live)).toBeNull();
    expect(modelOf(live).candidates.candidates.map(idea => idea.outcome)).toEqual(['running']);
  });
});

describe('the same run opened from its summary', () => {
  const from = modelOf(summarized(ended('failed'), fixture.events));
  const whole = modelOf(fold(ended('failed'), [fixture.events]));

  test('is closed as the whole history closes it', () => {
    expect(from.candidates.candidates).toEqual(whole.candidates.candidates);
    expect(from.agents.agents).toEqual(whole.agents.agents);
    expect(from.latest).toBe(whole.latest);
    expect(nowView(from)).toEqual([]);
  });

  test('holds the failed input gates as the whole history does', () => {
    expect(from.inputGates).toEqual(whole.inputGates);
    expect(heroView(from).tiles[0]?.detail).toBe('input not measured');
  });

  test('says the whole reason when the registry record carries only its cut', () => {
    const state = summarized(ended('failed'), fixture.events);
    expect(endedView(state)?.reason).toBe(FULL_REASON);
  });

  test('a view at an earlier sequence holds the gates the journal would hold then', () => {
    const state = summarized(ended('failed'), fixture.events);
    const gates = state.run.trace.inputGates;
    const first = gates.failures[0]?.sequence ?? 0;
    expect(failuresAt(gates, first - 1)).toEqual([]);
    expect(failuresAt(gates, first)).toHaveLength(1);
  });
});

describe('how the run ended settles what it left open', () => {
  test('a stopped run reads stopped, and one that finished reads done', () => {
    const stopped = modelOf(fold(ended('stopped'), [fixture.events]));
    const [idea] = stopped.candidates.candidates;
    expect(idea === undefined ? '' : outcomeWords(idea)).toBe('failed · run stopped');
    expect(stopped.trace.spans.get('flow:000001/#1')?.status).toBe('stopped');
    expect(
      modelOf(fold(ended('completed'), [fixture.events])).trace.spans.get('flow:000001/#1')?.status,
    ).toBe('done');
  });

  test('the same page whatever batches the events arrive in, and wherever the crash cut the journal', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const cut = 1 + random.int(fixture.events.length);
      const events = fixture.events.slice(0, cut);
      const whole = modelOf(fold(ended('failed'), [events]));
      const parts = modelOf(fold(ended('failed'), inBatches(events, random)));
      expect(parts.candidates.candidates, `seed ${seed}`).toEqual(whole.candidates.candidates);
      expect(parts.agents.agents, `seed ${seed}`).toEqual(whole.agents.agents);
      expect(parts.latest).toBe(whole.latest);
      expect(parts.inputGates).toEqual(whole.inputGates);
      // Whatever prefix the run reached, none of it still runs once the run is over.
      expect(whole.candidates.candidates.every(idea => idea.outcome !== 'running')).toBe(true);
      expect(whole.agents.agents.every(agent => agent.status !== 'running')).toBe(true);
      expect(nowView(whole)).toEqual([]);
      for (const idea of whole.candidates.candidates) {
        expect(idea.endedAt ?? Number.NaN).toBeGreaterThanOrEqual(idea.startedAt);
      }
    }
  });
});
