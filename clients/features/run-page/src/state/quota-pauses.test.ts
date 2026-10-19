import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event as fixtureEvent} from '@vibesys/contracts/testing';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {initialCoreState, reduceEvent, reduceEventBatch} from '@vibesys/run-state';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {attached, events, isoAt, RUN, spanEvent, step, T0} from './live-run-fixtures.js';
import {
  pausedSeconds,
  pauseWords,
  piecesAround,
  type QuotaPauseSpan,
  quotaPauses,
  workedSeconds,
} from './quota-pauses.js';
import {createFactsReader} from './run-facts.js';
import {createRunReader} from './run-model.js';
import {attemptLines, stepSegments, timelineRows} from './timeline-rows.js';

/**
 * A router turn that sat out a provider's quota for 37 minutes (`quota_resumed` says how long a stop
 * held the turn, stamped when it ended). Its time is not agent work: no duration counts it, and its
 * bar shows the pause as one of its own.
 */

function pause(from: number, to: number): QuotaPauseSpan {
  return {from, to, provider: 'claude', condition: 'quota_exhausted'};
}

describe('the time inside pauses', () => {
  test('is counted once, however the pauses overlap, and never beyond the stretch', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const pauses = Array.from({length: random.int(6)}, () => {
        const from = random.int(1000);
        return pause(from, from + random.int(400));
      });
      const start = random.int(800);
      const end = start + random.int(800);
      const paused = pausedSeconds(pauses, start, end);
      expect(paused, `seed ${seed}`).toBeGreaterThanOrEqual(0);
      expect(paused, `seed ${seed}`).toBeLessThanOrEqual(end - start);
      expect(workedSeconds(pauses, start, end), `seed ${seed}`).toBe(end - start - paused);
      // The same pauses in any order, and each twice, change nothing.
      const shuffled = [...pauses, ...pauses].sort(() => random.next() - 0.5);
      expect(pausedSeconds(shuffled, start, end), `seed ${seed}`).toBe(paused);
      // Worked time adds up over a split point.
      const middle = start + random.int(end - start + 1);
      expect(
        workedSeconds(pauses, start, middle) + workedSeconds(pauses, middle, end),
        `seed ${seed}`,
      ).toBeCloseTo(workedSeconds(pauses, start, end), 9);
      // The pieces tile the stretch, and their worked pieces are its worked time.
      const pieces = piecesAround(pauses, start, end);
      expect(pieces[0]?.start).toBe(start);
      expect(pieces.at(-1)?.end).toBe(Math.max(start, end));
      for (let at = 1; at < pieces.length; at += 1) {
        expect(pieces[at]?.start, `seed ${seed}`).toBe(pieces[at - 1]?.end ?? Number.NaN);
      }
      const worked = pieces.filter(piece => !piece.paused);
      expect(worked.reduce((sum, piece) => sum + (piece.end - piece.start), 0)).toBeCloseTo(
        workedSeconds(pauses, start, end),
        9,
      );
    }
  });
});

const ROUTER_TURN = (end: number): Span =>
  ({
    id: 'op:router',
    parent: 'flow:1/#1',
    type: 'operation',
    name: 'router',
    status: 'done',
    started_at: T0 + 100,
    ended_at: T0 + end,
    label: {
      kind: 'agent_turn',
      title: 'Router',
      attrs_json: JSON.stringify({agent: 'router-1', role: 'router'}),
    },
  }) as Span;

const WORKSTREAM = (end: number): Span =>
  ({
    id: 'flow:1/#1',
    parent: null,
    type: 'flow',
    name: 'idea',
    status: 'done',
    started_at: T0,
    ended_at: T0 + end,
    label: {
      kind: 'workstream',
      title: 'Idea 1',
      verdict: 'failed',
      attrs_json: JSON.stringify({hypothesis_id: 'h1', round: 1}),
    },
  }) as Span;

/** The page after a run whose router turn ran 100s to 2400s and waited 2220s of it on quota. */
function pausedRun() {
  const resumed: RunEvent = {
    ...fixtureEvent(3, 'quota_resumed', {
      data: {
        kind: 'quota_resumed',
        provider: 'claude',
        condition: 'quota_exhausted',
        reason: 'wait_elapsed',
        waited_seconds: 2220,
      },
    }),
    run_id: RUN.runId,
    timestamp: isoAt(T0 + 2380),
  };
  const batch = [
    spanEvent(1, T0, 'span_started', WORKSTREAM(2500)),
    spanEvent(2, T0 + 100, 'span_started', ROUTER_TURN(2400)),
    resumed,
    spanEvent(4, T0 + 2400, 'span_ended', ROUTER_TURN(2400)),
    spanEvent(5, T0 + 2500, 'span_ended', WORKSTREAM(2500)),
  ];
  // The session folds the run's core and hands it to the page; the page folds the trace itself.
  const state = step(attached(), [
    events(0, batch),
    {type: 'run-core', core: batch.reduce(reduceEvent, initialCoreState())},
  ]);
  const facts = createFactsReader()(state.run);
  return {state, facts, model: createRunReader().read(state.run.trace, null, null, facts)};
}

describe('a turn that waited on its quota', () => {
  test('places the stop on the run clock from the host’s stamps', () => {
    const {facts} = pausedRun();
    expect(facts.pauses).toHaveLength(1);
    const [only] = facts.pauses;
    // The spans' times and their events' stamps agree here, so the run clock reads as the host's.
    expect(only?.to).toBeCloseTo(T0 + 2380, 0);
    expect((only?.to ?? 0) - (only?.from ?? 0)).toBe(2220);
  });

  test('cuts the turn’s bar around the pause and counts only the work in its duration', () => {
    const {model} = pausedRun();
    const [router] = model.candidates.candidates[0]?.attempts[0]?.steps ?? [];
    expect(router?.endedAt).toBe(T0 + 2400);
    const segments = stepSegments(router ? [router] : [], null, model.pauses);
    expect(segments.map(segment => segment.kind)).toEqual(['solid', 'grey', 'solid']);
    expect(segments[1]?.label).toBe('Quota pause · 37m');
    expect(segments[0]?.label).toContain('· 1m');
    // Every piece belongs to its step, and the pause does not claim to be agent work.
    expect(segments.every(segment => segment.step === router?.span)).toBe(true);
    expect(segments[1]?.role).toBeNull();
    const total = segments.reduce((sum, segment) => sum + (segment.end - segment.start), 0);
    expect(total).toBe(2300);
  });

  test('does not count the pause in the idea’s attempt duration', () => {
    const {model} = pausedRun();
    const idea = model.candidates.candidates[0];
    const [line] = idea === undefined ? [] : attemptLines(idea, null, '', model.pauses);
    // The attempt took 2500s, 2220s of it paused: 4m 40s of work.
    expect(line?.took).toBe('4m');
    const unpaused = idea === undefined ? [] : attemptLines(idea, null, '');
    expect(unpaused[0]?.took).toBe('41m');
  });

  test('draws a Timeline row whose segments tile the idea with the pause among them', () => {
    const {model} = pausedRun();
    const row = timelineRows(model, 'all', {start: T0, now: null}, false).rows[0];
    expect(row?.segments.some(segment => segment.label.startsWith('Quota pause'))).toBe(true);
  });

  test('leaves an unpaused run’s bars whole', () => {
    const core = initialCoreState();
    const state = step(attached(), [
      events(0, [
        spanEvent(1, T0, 'span_started', WORKSTREAM(2500)),
        spanEvent(2, T0 + 2400, 'span_ended', ROUTER_TURN(2400)),
      ]),
    ]);
    expect(core.quotaStops).toEqual([]);
    const facts = createFactsReader()(state.run);
    expect(facts.pauses).toEqual([]);
  });

  test('is left out when the run clock has no stamp to relate it to', () => {
    const core = reduceEvent(initialCoreState(), {
      ...fixtureEvent(1, 'quota_resumed', {
        data: {kind: 'quota_resumed', provider: 'claude', waited_seconds: 60},
      }),
      timestamp: isoAt(T0 + 100),
    });
    expect(quotaPauses(core.quotaStops, null)).toEqual([]);
    expect(quotaPauses(core.quotaStops, 0)).toHaveLength(1);
  });
});

describe('the quota-pause run fixture, folded as the page folds a live run', () => {
  const fixture = fixtureNamed('quota-pause').events as RunEvent[];
  const state = step(attached(), [
    events(0, fixture),
    {type: 'run-core', core: reduceEventBatch(initialCoreState(), fixture)},
  ]);
  const facts = createFactsReader()(state.run);
  const model = createRunReader().read(state.run.trace, null, null, facts);

  test('holds the 37-minute stop as one pause inside the second idea’s implementer turn', () => {
    expect(facts.pauses.map(each => each.to - each.from)).toEqual([2220]);
    const [, second] = model.candidates.candidates;
    const turn = second?.attempts[0]?.steps.find(each => each.role === 'implementer');
    const [pause] = facts.pauses;
    expect(pause?.from ?? 0).toBeGreaterThan(turn?.startedAt ?? Number.POSITIVE_INFINITY);
    expect(pause?.to ?? Number.POSITIVE_INFINITY).toBeLessThan(turn?.endedAt ?? 0);
  });

  test('leaves the paused time out of the idea’s duration', () => {
    const [, second] = model.candidates.candidates;
    const took = (second?.endedAt ?? 0) - (second?.startedAt ?? 0);
    expect(took).toBe(5000);
    expect(workedSeconds(model.pauses, second?.startedAt ?? 0, second?.endedAt ?? 0)).toBe(2780);
  });
});

describe('a pause that outlasts its wait', () => {
  const at = (clock: string): string => `2026-10-19T${clock}Z`;
  const quotaEvent = (
    sequence: number,
    clock: string,
    data: NonNullable<RunEvent['data']>,
  ): RunEvent => ({
    ...fixtureEvent(sequence, data?.kind as RunEvent['type'], {data}),
    timestamp: at(clock),
  });
  const status = (
    sequence: number,
    clock: string,
    to: 'pausing' | 'paused' | 'running',
    previous: 'running' | 'pausing' | 'paused',
  ): RunEvent => quotaEvent(sequence, clock, {kind: 'run_status_changed', status: to, previous});
  const seconds = (clock: string): number => Date.parse(at(clock)) / 1000;

  /** The journal of a live run: waited 14 minutes, then paused until an operator resumed it. */
  const JOURNAL: RunEvent[] = [
    quotaEvent(1, '13:55:34', {
      kind: 'quota_paused',
      provider: 'claude',
      condition: 'quota_exhausted',
      detail: 'usage limit',
    }),
    status(2, '13:55:34', 'paused', 'running'),
    quotaEvent(3, '14:09:51', {
      kind: 'quota_abandoned',
      provider: 'claude',
      condition: 'quota_exhausted',
      detail: 'usage limit',
      reason: 'budget',
      waited_seconds: 857,
    }),
    status(4, '14:32:58', 'running', 'paused'),
  ];

  test('runs from the pause until the run resumed, not for the wait alone', () => {
    const core = reduceEventBatch(initialCoreState(), JOURNAL);
    const [pause] = quotaPauses(core.quotaStops, 0);
    expect(pause?.from).toBe(seconds('13:55:34'));
    expect(pause?.to).toBe(seconds('14:32:58'));
    expect(pauseWords((pause?.to ?? 0) - (pause?.from ?? 0))).toBe('Quota pause · 37m');
  });

  test('is not closed by the stop that ended while the run was still paused', () => {
    const core = reduceEventBatch(initialCoreState(), JOURNAL.slice(0, 3));
    const [stop] = core.quotaStops;
    expect(stop?.resumedAt).toBeNull();
    // Until the run resumes the pause runs to the stop's end, the latest time it is known to last.
    const [pause] = quotaPauses(core.quotaStops, 0);
    expect(pause?.to).toBe(seconds('14:09:51'));
  });

  test('is, over random pauses, the time the run was paused: paused to running', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      let clock = seconds('09:00:00');
      let sequence = 0;
      const next = (): number => {
        sequence += 1;
        return sequence;
      };
      const journal: RunEvent[] = [];
      let expected = 0;
      const stamp = (): string => new Date(clock * 1000).toISOString().slice(11, 19);
      for (let each = 0; each <= random.int(4); each += 1) {
        clock += 1 + random.int(600);
        const pausedAt = clock;
        journal.push(
          quotaEvent(next(), stamp(), {
            kind: 'quota_paused',
            provider: 'claude',
            condition: 'quota_exhausted',
            detail: 'limit',
          }),
          status(next(), stamp(), 'paused', 'running'),
        );
        const waited = 1 + random.int(900);
        clock += waited;
        journal.push(
          quotaEvent(
            next(),
            stamp(),
            random.bool()
              ? {
                  kind: 'quota_resumed',
                  provider: 'claude',
                  condition: 'quota_exhausted',
                  reason: 'wait_elapsed',
                  waited_seconds: waited,
                }
              : {
                  kind: 'quota_abandoned',
                  provider: 'claude',
                  condition: 'quota_exhausted',
                  detail: 'limit',
                  reason: 'budget',
                  waited_seconds: waited,
                },
          ),
        );
        // The run resumes at once, or only when an operator does, long after the wait ended.
        clock += random.bool() ? 0 : random.int(3000);
        journal.push(status(next(), stamp(), 'running', 'paused'));
        expected += clock - pausedAt;
      }
      const core = reduceEventBatch(initialCoreState(), journal);
      const pauses = quotaPauses(core.quotaStops, 0);
      const total = pauses.reduce((sum, pause) => sum + (pause.to - pause.from), 0);
      expect(total, `seed ${seed}`).toBe(expected);
      expect(pausedSeconds(pauses, 0, Number.MAX_SAFE_INTEGER), `seed ${seed}`).toBe(expected);
    }
  });
});
