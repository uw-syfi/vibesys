import {describe, expect, test} from 'bun:test';
import type {RunOverview, Span} from '@vibesys/contracts';
import type {HostRun, RunEnd} from '@vibesys/hosts';
import {EMPTY_TRACE, foldTrace} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {adoptedCandidate, endReason, unclipped} from './end-words.js';
import {startHistory} from './history-pager.js';
import {EMPTY_RUN_FOLD, traceLog} from './live.js';
import {type RunPageState, runPageInitial} from './run-page.js';
import {endedView} from './views.js';

type Outcome = NonNullable<RunEnd>['outcome'];

const OUTCOMES: readonly Outcome[] = [
  'adopted',
  'kept-input',
  'no-winner',
  'adoption-failed',
  'finish-refused',
  'stopped',
  'deadline',
  'failed',
  'force_stopped',
];
/** Core's own account of an ending: its code and what it was about, in its terms. */
const CORE_REASONS = [
  'adopted: settlement:000002/settle.1',
  'kept-input: the input',
  'stopped: kept-input: the input',
  'no-winner: every round failed',
  'adoption-failed: verify_failed',
  'finish-refused: run_closed',
  'deadline: kept-input: the input',
];

function span(id: string, parent: string | null, extra: Partial<Span>): Span {
  return {
    id,
    parent,
    type: 'flow',
    name: id,
    status: 'done',
    waiting_on: [],
    attempts: [],
    started_at: 0,
    ended_at: 1,
    failure: null,
    stop: null,
    ...extra,
  } as Span;
}

function round(number: number, value: number, vs: number, verdict = 'passed'): Span[] {
  const id = `flow:round${number}#1`;
  return [
    span(id, 'flow:root#1', {
      label: {
        kind: 'workstream',
        title: `round ${number}`,
        attrs_json: JSON.stringify({round: number, hypothesis_id: `H${number}`}),
        links: [],
      },
    }),
    span(`op:gate${number}`, id, {
      type: 'operation',
      label: {
        kind: 'gate',
        title: 'benchmark',
        attrs_json: JSON.stringify({stage: 'benchmark', metric_value: value, vs_baseline: vs}),
        verdict,
        links: [],
      },
    }),
  ];
}

function ended(
  outcome: Outcome,
  reason: string,
  spans: readonly Span[],
  direction: 'max' | 'min' = 'max',
): RunPageState {
  const run: HostRun = {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/queue-rs',
    runId: 'run',
    status: 'completed',
    phase: 'completed',
    paused: null,
    end: {outcome, reason, resumable: false},
    startedAt: 1,
    reason: null,
  };
  const overview = {
    objective: {text: 'Go faster.', direction},
  } as RunOverview;
  const trace = foldTrace(
    EMPTY_TRACE,
    spans.map((each, index) => ({
      sequence: index + 1,
      type: 'span_started',
      data: {kind: 'span_started', span: each},
    })) as never,
  );
  return {
    ...runPageInitial({
      identity: {host: 'ssh:host-b', runId: 'run'},
      title: {project: 'queue-rs', task: 'mpmc'},
      hostLabel: 'host-b',
      unavailable: {},
    }),
    record: {kind: 'ended', run},
    run: {...EMPTY_RUN_FOLD, trace: traceLog(EMPTY_TRACE, overview, [])},
    ...(spans.length === 0 ? {} : {run: {...EMPTY_RUN_FOLD, trace: traceLog(trace, overview, [])}}),
  };
}

const ROOT = span('flow:root#1', null, {name: 'multi'});

/** The search's own account of what it adopted: the candidate and how it measured. */
function adoptionOf(value: number, vs: number): Span {
  return span('op:adopt', 'flow:root#1', {
    type: 'operation',
    label: {
      kind: 'adoption',
      title: 'adopt',
      attrs_json: JSON.stringify({candidate: 'abc123', metric_value: value, vs_baseline: vs}),
      verdict: 'adopted',
      links: [],
    },
  });
}

describe('the ended banner', () => {
  test('says which candidate was adopted, from which round, and how much faster', () => {
    const state = ended('adopted', 'adopted: settlement:000002/settle.1', [
      ROOT,
      ...round(1, 100, 1.4),
      ...round(2, 410, 5.7234),
      ...round(3, 50, 0.6, 'slower'),
      adoptionOf(410, 5.7234),
    ]);
    expect(endedView(state)?.reason).toBe(
      'Adopted the candidate from round 2, 5.72× faster than the input.',
    );
  });

  test('names no best from its gates when the search labels no adoption', () => {
    const gates = [ROOT, ...round(1, 100, 1.4), ...round(2, 410, 5.7234), ...round(3, 900, 12)];
    expect(endedView(ended('adopted', 'adopted: x', gates))?.reason).toBe(
      'Adopted the best candidate.',
    );
  });

  test('names the candidate the search adopted, not the best gate it saw', () => {
    const adopted = span('op:adopt', 'flow:root#1', {
      type: 'operation',
      label: {
        kind: 'adoption',
        title: 'adopt',
        attrs_json: JSON.stringify({candidate: 'abc123', metric_value: 410, vs_baseline: 5.7234}),
        verdict: 'adopted',
        links: [],
      },
    });
    const state = ended('adopted', 'adopted: settlement:000002/settle.1', [
      ROOT,
      ...round(1, 100, 1.4),
      ...round(2, 410, 5.7234),
      ...round(3, 900, 12, 'passed'),
      adopted,
    ]);
    expect(endedView(state)?.reason).toBe(
      'Adopted the candidate from round 2, 5.72× faster than the input.',
    );
  });

  test('says the input stood when the search adopted no candidate', () => {
    const input = span('op:adopt', 'flow:root#1', {
      type: 'operation',
      label: {kind: 'adoption', title: 'adopt', attrs_json: '{}', verdict: 'adopted', links: []},
    });
    const state = ended('adopted', 'adopted: the input', [ROOT, ...round(1, 100, 1.4), input]);
    expect(endedView(state)?.reason).toBe('No candidate beat the input, so it stands as it was.');
  });

  test('reads a lower-is-better objective the same way', () => {
    const state = ended(
      'adopted',
      'adopted: s',
      [ROOT, ...round(1, 25, 0.25), adoptionOf(25, 0.25)],
      'min',
    );
    expect(endedView(state)?.reason).toBe(
      'Adopted the candidate from round 1, 4.00× faster than the input.',
    );
  });

  test('says no more than it knows', () => {
    expect(endedView(ended('adopted', 'adopted: x', []))?.reason).toBe(
      'Adopted the best candidate.',
    );
    const slower = ended('adopted', 'adopted: x', [
      ROOT,
      ...round(1, 90, 0.9),
      adoptionOf(90, 0.9),
    ]);
    expect(endedView(slower)?.reason).toBe('Adopted the candidate from round 1.');
  });

  test('a run whose input stands says so', () => {
    expect(endedView(ended('kept-input', 'kept-input: the input', [ROOT]))?.reason).toBe(
      'No candidate beat the input, so it stands as it was.',
    );
  });

  test('never shows core’s own account of an ending, for any outcome or trace', () => {
    for (const outcome of OUTCOMES.filter(each => each !== 'failed' && each !== 'force_stopped')) {
      for (const reason of CORE_REASONS) {
        for (const stands of [null, adoptedCandidate([ROOT, ...round(2, 410, 5.7)])]) {
          const words = endReason(outcome, reason, stands, 'max');
          expect(words).not.toContain(reason);
          expect(words).not.toMatch(/settlement:|kept-input|no-winner|[a-z]+_[a-z]+/);
        }
      }
    }
  });

  test('claims no final verdict while the history is still being read', () => {
    // Regression: a prefix of an opened run's history read as "Adopted the candidate from round 1".
    const rounds = [1, 2, 3, 4, 5, 6].map(n => round(n, 100 * n, 1 + n));
    const full = [ROOT, ...rounds.flat(), adoptionOf(600, 7)];
    for (let count = 0; count <= rounds.length; count += 1) {
      const prefix = [ROOT, ...rounds.slice(0, count).flat(), adoptionOf(100 * count, 1 + count)];
      const reading = {
        ...ended('adopted', 'adopted: s', prefix),
        history: {
          kind: 'reading',
          pager: startHistory(1, 2000, 1).pager,
          progress: {events: 0, bytes: 0, total: null},
          core: null,
        },
      } as const;
      expect(endedView(reading)?.reason).toMatch(/^Reading the run.s history/);
    }
    const read = {...ended('adopted', 'adopted: s', full), history: {kind: 'read'}} as const;
    expect(endedView(read)?.reason).toMatch(/^Adopted the candidate from round /);
  });

  test('says an adopted candidate was never compared with the input, however the run ended', () => {
    for (const outcome of ['adopted', 'deadline', 'stopped'] as const) {
      const words = endReason(outcome, `${outcome}: adopted: c1`, null, 'max', true);
      expect(words).toContain('adopted without an input comparison');
      expect(endReason(outcome, `${outcome}: adopted: c1`, null, 'max')).not.toContain(
        'input comparison',
      );
    }
  });

  test('shows a failure’s own message', () => {
    expect(endReason('failed', 'W6’s benchmark timed out after 300 s.', null, 'max')).toBe(
      'W6’s benchmark timed out after 300 s.',
    );
  });
});

describe('unclipped', () => {
  const CUT = 200;

  /** The registry's cut of `full`: its first 199 characters and an ellipsis, when it is longer. */
  function cut(full: string): string {
    return full.length <= CUT ? full : `${full.slice(0, CUT - 1)}\u2026`;
  }

  test('gives the whole failure when the end record holds only the registry’s cut of it', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const full = `Failure ${'w'.repeat(random.int(500))}: ${seed}`;
      expect(unclipped(cut(full), full), `seed ${seed}`).toBe(full);
    }
  });

  test('keeps the end record’s reason when the failure is not the same account', () => {
    expect(unclipped('stopped by you', 'something else')).toBe('stopped by you');
    expect(unclipped(`${'a'.repeat(199)}\u2026`, 'b'.repeat(300))).toBe(`${'a'.repeat(199)}\u2026`);
    expect(unclipped(`${'a'.repeat(199)}\u2026`, null)).toBe(`${'a'.repeat(199)}\u2026`);
  });
});
