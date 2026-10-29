import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace, traceSpans} from '@vibesys/run-state/trace';
import {predatesTraceWords, roundsBeforeTrace} from './predates-trace.js';
import {type RunPageState, runPageInitial, runPageStep} from './run-page.js';
import {summaryFacts, type WireSummary} from './run-summary.js';
import {logsNotice} from './views.js';

const FIXTURES = resolve(import.meta.dir, '../../../../../tests/conformance/fixtures/run-summary');

interface Fixture {
  readonly events: readonly RunEvent[];
  readonly summary: {
    readonly through_sequence: number;
    readonly event_count: number;
    readonly trace: 'recorded' | 'predates_trace';
    readonly rounds_finished: number;
    readonly spans: readonly unknown[];
    readonly overview: unknown;
  };
}

function fixtureNamed(name: string): Fixture {
  return JSON.parse(readFileSync(resolve(FIXTURES, name), 'utf8')) as Fixture;
}

/** The helper's summary of a journal, as the fixture the Python tests hold the helper to words it. */
function wire(name: string): WireSummary {
  const {summary} = fixtureNamed(name);
  const none = {input: null, cacheRead: null, cacheWrite: null, output: null, reasoning: null};
  return {
    throughSequence: summary.through_sequence,
    eventCount: summary.event_count,
    journalBytes: 1,
    trace: summary.trace,
    roundsFinished: summary.rounds_finished,
    overview: summary.overview,
    spans: summary.spans,
    replies: [],
    tokens: none,
    usage: [],
    records: [],
    inputGates: [],
    terminal: null,
  };
}

const RUN_ID = '20261004-000000-0a0a0a0a-dynorch-legacy';

function opened(summary: WireSummary): RunPageState {
  const initial = runPageInitial({
    identity: {host: 'ssh:host-b', runId: RUN_ID},
    title: {project: 'queue-rs', task: null},
    hostLabel: 'host-b',
    unavailable: {},
  });
  const asked = runPageStep(initial, {type: 'record', record: {kind: 'historical', status: null}});
  const request = asked.requests.find(each => each.kind === 'read-summary');
  if (request?.kind !== 'read-summary') throw new Error('no summary asked');
  return runPageStep(asked.state, {
    type: 'summary',
    token: request.token,
    reply: {kind: 'summary', ...summaryFacts(summary, RUN_ID, 'stopped')},
    wallMs: 0,
  }).state;
}

describe('a saved run recorded before the run trace', () => {
  test('is known as such from its summary, with the rounds it recorded', () => {
    expect(roundsBeforeTrace(opened(wire('predates-trace.json')))).toBe(2);
  });

  test('has no spans for the client to fold from its journal, as the summary says', () => {
    const {events, summary} = fixtureNamed('predates-trace.json');
    expect(events.length).toBeGreaterThan(0);
    expect(traceSpans(foldTrace(EMPTY_TRACE, events))).toEqual([]);
    expect(summary.spans).toEqual([]);
  });

  test('a run with a trace is not, whatever its rounds', () => {
    expect(roundsBeforeTrace(opened(wire('two-ideas.json')))).toBeNull();
  });

  test('says what it lacks, naming the rounds and never calling the run empty', () => {
    const words = predatesTraceWords(2);
    expect(words.title).toBe('Recorded by an older VibeSys');
    expect(words.body).toContain('not available');
    expect(words.body).toContain('2 rounds');
    expect(predatesTraceWords(1).body).toContain('1 round,');
    expect(`${words.title} ${words.body}`).not.toMatch(/no ideas|nothing measured|stopped/i);
  });
});

describe('the Logs tab of a saved run', () => {
  test('says no logs can be read instead of looking for them without end', () => {
    const state = opened(wire('two-ideas.json'));
    expect(state.record.kind).toBe('historical');
    expect(state.logs.kind).toBe('idle');
    const notice = logsNotice(state);
    expect(notice).toContain('logs are not available');
    expect(notice).not.toContain('Looking');
  });

  test('still looks while a run the app is looking for has not been found', () => {
    const initial = runPageInitial({
      identity: {host: 'ssh:host-b', runId: RUN_ID},
      title: {project: 'queue-rs', task: null},
      hostLabel: 'host-b',
      unavailable: {},
    });
    expect(logsNotice(initial)).toBe('Looking for the run’s logs…');
  });
});
