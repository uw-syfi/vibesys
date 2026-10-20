/**
 * The synthetic scenarios: runs that show the cases a screen must survive and that a recording may
 * not hold. Each is a pure function of nothing (no clock, no randomness), so the committed JSON in
 * `data/` is exactly what these write and a test holds them equal. The 14-day run, too large to
 * commit, is `weeks.ts`.
 */
import type {RunFixture} from './fixture.js';
import {parseFixture} from './fixture.js';
import {
  addIdea,
  addJudgeTurn,
  adoptionSpan,
  type GateReading,
  type IdeaSpec,
  MODEL_A,
  planningSpan,
  rootSpan,
  Trace,
  type TurnUsage,
  toolCall,
  turnTranscript,
} from './trace.js';

const OBJECTIVE = {
  text: 'Maximize trusted end-to-end throughput of the MPMC queue. Every candidate must pass the 100-trial correctness check.',
  metric: 'total_ops_per_sec',
  unit: 'ops/s',
  direction: 'max',
} as const;

/** One axis a run with several objectives ranks its candidates on, as its overview declares it. */
interface Axis {
  readonly name: string;
  readonly direction: 'max' | 'min';
}

export function runHeader(trace: Trace, runId: string, objectives: readonly Axis[] = []): void {
  trace.event(
    'run_started',
    {
      outer_loop: 'iterate',
      input: 'objectives/queue.md',
      max_rounds: 6,
      expected_roles: ['orchestrator', 'implementer', 'judge'],
      // The run declares what it optimizes, as a real run's first event does.
      overview: {
        objective: {
          ...OBJECTIVE,
          ...(objectives.length === 0 ? {} : {objectives}),
          correctness_check: [],
        },
      },
    },
    0,
  );
  trace.event(
    'run_configured',
    {
      run_log_path: `logs/${runId}`,
      project_root: 'project',
      model: MODEL_A,
      objective: 'Raise queue throughput',
      search_policy: 'beam',
      benchmark_contract: true,
      pareto_objectives: null,
      max_continuation_rounds: 3,
      max_unmeasured_turns: null,
      source: 'loop',
    },
    1,
  );
  trace.span('span_started', rootSpan('running'), 2);
}

interface Scenario {
  readonly name: string;
  readonly description: string;
  readonly runId: string;
  readonly state: 'live' | 'finished';
  /** The axes the run declares when it has several objectives. */
  readonly objectives?: readonly Axis[];
  readonly build: (trace: Trace) => void;
  readonly liveAfter?: (trace: Trace) => number;
  readonly dropAfter?: (trace: Trace) => number;
}

function fixture(scenario: Scenario): RunFixture {
  const trace = new Trace(scenario.runId);
  runHeader(trace, scenario.runId, scenario.objectives);
  scenario.build(trace);
  const finished = scenario.state === 'finished';
  if (finished) {
    trace.event('run_finished', null, 4000, {status: 'completed'});
  }
  return parseFixture({
    format: 1,
    name: scenario.name,
    description: scenario.description,
    runId: scenario.runId,
    state: scenario.state,
    objective: OBJECTIVE,
    events: trace.events,
    ...(scenario.liveAfter === undefined ? {} : {liveAfter: scenario.liveAfter(trace)}),
    ...(scenario.dropAfter === undefined ? {} : {dropAfter: scenario.dropAfter(trace)}),
    ...(finished ? {end: {outcome: 'adopted', reason: 'adopted the best'}} : {}),
  });
}

function plan(trace: Trace, n: number, at: number): void {
  const span = planningSpan(n, at);
  trace.span('span_started', {...span, status: 'running'}, at);
  trace.span('span_ended', span, at + 40);
}

const LONG_SENTENCE =
  'Replace the per-slot sequence counters with a single padded head and tail pair, batch the producer-side reservation across up to sixteen enqueues, park idle consumers on a futex after a bounded spin, and re-measure every contention level from one to sixty-four threads before keeping anything';
const LONG_TOKEN = 'a'.repeat(96);

/** Ideas whose titles are as long, as wide or as strange as an agent makes them. */
function longTitles(): RunFixture {
  const titles = [
    LONG_SENTENCE,
    `Ring${LONG_TOKEN}Buffer`,
    '並列キューのためのキャッシュライン整列とバッチ処理 🚀 sequence ring',
    'Use `crossbeam::utils::CachePadded` for `head` and `tail`, then `Relaxed` loads where the fence allows',
    'Short one',
    'Lock-free ring with per-slot sequence numbers (v3, after the review asked for a stricter memory ordering on the consumer side of the ring)',
    'Tiny',
    'Batch enqueue → combine → steal: a three-stage pipeline with a work-stealing deque per core and a global overflow list that is drained only when every local deque is empty',
  ];
  return fixture({
    name: 'long-titles',
    description: 'Ideas whose titles are very long, one unbroken token, wide characters or markup.',
    runId: '20261015-163047',
    state: 'live',
    build: trace => {
      plan(trace, 1, 10);
      titles.forEach((title, index) => {
        const n = index + 1;
        const end =
          index % 3 === 0
            ? ({kind: 'passed', value: 1.2e7 + n * 1e6, ratio: 1.2 + n / 10} as const)
            : index % 3 === 1
              ? ({kind: 'slower', value: 0.9e7, ratio: 0.9} as const)
              : ({kind: 'running'} as const);
        addIdea(trace, {n, title, at: 100 + n * 20, end, took: 400});
      });
      trace.span('span_started', adoptionSpan({n: 1, ratio: 1.3, value: 1.3e7}, 900, false), 900);
    },
  });
}

/** The failure reasons a workstream span can carry, in the wire's spellings. */
export const FAILURE_REASONS: readonly string[] = [
  'turned_down',
  'not_measured',
  'no_design',
  'not_opened',
  'stopped',
  'parent-unverified',
  'refused',
  'ended',
  'out-of-budget',
  'turn-failed',
  'invalid-reply',
  'render-failed',
  'wait-refused',
  'checkpoint-declined',
  'no-changed-candidate',
  'implementation-failed',
  'review-rejected',
  'no-trusted-evidence',
  'evidence-refused',
  'evidence-undecodable',
  'profile-capture-unavailable',
  'turn-limit',
  'repeated-turn',
  'merge-refused',
  'merge-unsound',
  'merge-regressed',
];

/** One idea per failure reason, and one per way a gate rejects a candidate. */
function failureReasons(): RunFixture {
  return fixture({
    name: 'failure-reasons',
    description:
      'Failed ideas: one for each failure reason the search spells, and the gate rejections.',
    runId: '20261015-171200',
    state: 'live',
    build: trace => {
      plan(trace, 1, 10);
      let n = 0;
      for (const reason of FAILURE_REASONS) {
        n += 1;
        addIdea(trace, {
          n,
          title: `Idea ${n}: ${reason.replace(/[-_]/g, ' ')} case`,
          at: 100 + n * 15,
          end: {kind: 'failed', reason},
          ...(reason.startsWith('implementation') ? {outcome: 'implementation_failed'} : {}),
          took: 200,
        });
      }
      const gates: readonly IdeaSpec['end'][] = [
        {kind: 'failed_check', check: 'correctness', completed: 93, required: 100, unit: 'trials'},
        {kind: 'failed_check', check: 'correctness'},
        {kind: 'failed_check', check: 'benchmark'},
        {kind: 'slower', value: 0.8e7, ratio: 0.8},
        {kind: 'passed', value: 1.4e7, ratio: 1.4},
      ];
      for (const end of gates) {
        n += 1;
        addIdea(trace, {n, title: `Gate case ${n}`, at: 100 + n * 15, end, took: 200});
      }
      // The implementer's reply for the first failed idea, as the server sends it.
      trace.event(
        'agent_execution_finished',
        {
          result: '{}',
          reply: {
            kind: 'ImplementerReply',
            json_schema: {
              properties: {
                summary: {type: 'string'},
                outcome: {
                  enum: ['nominated', 'implementation_failed', 'blocked'],
                  type: 'string',
                },
              },
              required: ['summary', 'outcome'],
              type: 'object',
            },
            value: {
              summary: 'The borrow checker rejected the shared slot; I could not restructure it.',
              outcome: 'implementation_failed',
            },
          },
        },
        900,
        {
          status: 'completed',
          invocation_id: 'inv-w16',
          agent_kind: 'implementer',
          span_id: 'op:w16-turn',
        },
      );
    },
  });
}

/** Ideas retried once, twice, many times, and one retrying now. */
function retries(): RunFixture {
  return fixture({
    name: 'retries',
    description:
      'Ideas whose implementer turn was retried: reasons, counts and one still retrying.',
    runId: '20261015-180000',
    state: 'live',
    build: trace => {
      plan(trace, 1, 10);
      const reasons = [
        'build failed: borrow error in lib.rs',
        'build failed: missing Send bound',
        'the correctness check timed out after 600 s',
        'the agent ended its turn without a reply',
        'build failed: type mismatch between the slot and its sequence counter',
      ];
      addIdea(trace, {
        n: 1,
        title: 'Settled after one retry',
        at: 100,
        tries: reasons.slice(0, 1),
        end: {kind: 'passed', value: 1.3e7, ratio: 1.3},
      });
      addIdea(trace, {
        n: 2,
        title: 'Settled after two retries',
        at: 110,
        tries: reasons.slice(0, 2),
        end: {kind: 'slower', value: 0.9e7, ratio: 0.9},
      });
      addIdea(trace, {
        n: 3,
        title: 'Gave up after four retries',
        at: 120,
        tries: reasons.slice(0, 4),
        end: {kind: 'failed', reason: 'turn-limit'},
      });
      addIdea(trace, {
        n: 4,
        title: 'Retrying now',
        at: 130,
        tries: reasons.slice(1, 3),
        end: {kind: 'running'},
      });
      addIdea(trace, {
        n: 5,
        title: 'Same reason four times',
        at: 140,
        tries: [reasons[0], reasons[0], reasons[0], reasons[0]] as string[],
        end: {kind: 'passed', value: 1.6e7, ratio: 1.6},
      });
      trace.span('span_started', adoptionSpan({n: 5, ratio: 1.6, value: 1.6e7}, 900, false), 900);
    },
  });
}

/** A search that plays rounds, including the continuation rounds past the planned ones. */
function continuationRounds(): RunFixture {
  return fixture({
    name: 'continuation-rounds',
    description:
      'Rounds one to six, the last three being continuation rounds, each starting from an earlier round.',
    runId: '20261015-190000',
    state: 'live',
    build: trace => {
      const rounds: readonly {end: IdeaSpec['end']; base: number | null}[] = [
        {end: {kind: 'passed', value: 1.3e7, ratio: 1.3}, base: null},
        {end: {kind: 'failed', reason: 'turned_down'}, base: 1},
        {end: {kind: 'passed', value: 1.7e7, ratio: 1.7}, base: 1},
        {end: {kind: 'slower', value: 1.5e7, ratio: 1.5}, base: 3},
        {end: {kind: 'passed', value: 2.1e7, ratio: 2.1}, base: 3},
        {end: {kind: 'running'}, base: 5},
      ];
      rounds.forEach((round, index) => {
        const n = index + 1;
        addIdea(trace, {
          n,
          title: `Round ${n}${n > 3 ? ' (continuation)' : ''}`,
          idea: `Try the ${['ring', 'padding', 'batching', 'parking', 'stealing', 'combining'][index]} design`,
          at: 100 + index * 700,
          round: n,
          baseRound: round.base,
          on: round.base,
          end: round.end,
        });
      });
      trace.span('span_started', adoptionSpan({n: 5, ratio: 2.1, value: 2.1e7}, 4200, false), 4200);
    },
  });
}

/**
 * A live run that shows where each idea started from and how the framework judged its effect:
 * one built on the best, one on a passed idea that is no longer the best, one from the input that
 * passed within noise, a continuation of its own work, a failed one that measured nothing (it kept
 * its base's revision), one whose comparison is still taking in runs and one just started.
 */
function basesAndEffects(): RunFixture {
  const ideas: readonly Omit<IdeaSpec, 'at'>[] = [
    {
      n: 1,
      title: 'Padded counters',
      round: 1,
      baseRound: null,
      end: {kind: 'passed', value: 1.2e7, ratio: 1.2},
      effect: 'better',
    },
    {
      n: 2,
      title: 'Batched enqueue on the padded counters',
      round: 2,
      baseRound: 1,
      on: 1,
      end: {kind: 'passed', value: 1.5e7, ratio: 1.5},
      effect: 'better',
    },
    {
      n: 3,
      title: 'Spin before parking',
      round: 3,
      baseRound: null,
      end: {kind: 'passed', value: 1.07e7, ratio: 1.07},
      effect: 'within_noise',
    },
    {
      n: 4,
      title: 'Per-core free lists',
      round: 4,
      baseRound: 1,
      on: 1,
      end: {kind: 'passed', value: 1.3e7, ratio: 1.3},
      effect: 'worse',
    },
    {
      n: 5,
      title: 'Widen the batch to sixty-four',
      round: 5,
      baseRound: 2,
      on: 2,
      end: {kind: 'failed', reason: 'turned_down'},
      // Changed nothing, so it kept idea 2's revision, which has a comparison of its own.
      keeps: 2,
    },
    {
      n: 6,
      title: 'Prefetch the next slot',
      round: 6,
      baseRound: 6,
      on: 2,
      end: {kind: 'passed', value: 1.9e7, ratio: 1.9},
      effect: 'better',
    },
    {
      n: 7,
      title: 'Combine adjacent slots',
      round: 7,
      baseRound: 7,
      on: 6,
      end: {kind: 'measuring', value: 1.95e7, ratio: 1.95},
      effect: 'within_noise',
    },
    {n: 8, title: 'Shard the free list', round: 8, baseRound: 6, on: 6, end: {kind: 'running'}},
  ];
  return fixture({
    name: 'bases-and-effects',
    description:
      'A live run whose ideas start from the best, from a passed idea, from the input and from their own work, with the framework verdict on each effect and the best named while the run works.',
    runId: '20261018-100000',
    state: 'live',
    build: trace => {
      plan(trace, 1, 10);
      ideas.forEach((idea, index) => {
        addIdea(trace, {...idea, at: 100 + index * 700});
      });
      trace.span('span_started', adoptionSpan({n: 6, ratio: 1.9, value: 1.9e7}, 5000, false), 5000);
    },
  });
}

/**
 * A run whose input's benchmark failed twice before it passed, so no idea has a ratio to the input,
 * and whose second idea sat out a provider's quota for 37 minutes. Each idea's gate states the
 * reading of one run; the framework's comparison states the mean of its runs, which is the value.
 */
function quotaPause(): RunFixture {
  const reason = `RuntimeError: ${'the engine failed its preflight '.repeat(5)}`.trim();
  const gate = (trace: Trace, at: number, failed: boolean): void => {
    trace.event(
      'gate_finished',
      {gate: 'benchmark', recipe: null, output_tail: failed ? `${reason}\n` : null},
      at,
      {status: failed ? 'failed' : 'completed'},
    );
  };
  return fixture({
    name: 'quota-pause',
    description:
      'A live run whose input benchmark failed and then passed, whose ideas have no ratio to the input but one to their base, and whose second idea waited 37 minutes on a provider quota.',
    runId: '20261019-120000',
    state: 'live',
    build: trace => {
      gate(trace, 5, true);
      gate(trace, 8, true);
      gate(trace, 12, false);
      plan(trace, 1, 10);
      addIdea(trace, {
        n: 1,
        title: 'Preallocate the arena',
        at: 100,
        round: 1,
        baseRound: null,
        end: {kind: 'passed', value: 350.39, sample: 352.1, ratio: 350.39 / 286.96, noInput: true},
        effect: 'better',
      });
      addIdea(trace, {
        n: 2,
        title: 'Pin the consumers to cores',
        at: 800,
        on: 1,
        round: 2,
        baseRound: 1,
        took: 5000,
        end: {kind: 'passed', value: 385.4, sample: 395.4, ratio: 385.4 / 350.4, noInput: true},
        effect: 'better',
        pause: {at: 3300, seconds: 2220},
      });
    },
  });
}

/**
 * A live run whose input benchmark failed before the first idea and never passed, with a long
 * report: the Benchmark card stands as a failure above the figures, and the run's id is as long as
 * a real one. A Rust benchmark prints a held value as `Some(8192)`.
 */
function inputGateFailed(): RunFixture {
  const tail = [
    'quick: warmup sub-run failed (this is where a prefix-cache-preflight failure surfaces):',
    'sessions arrival rate | saturated (recorded arrivals ignored, bounded by --max-concurrency 12)',
    'session workload | sessions=12 rounds=72 max_prompt_len=8497 max_prefix_len=6860',
    'Error: prefix-cache preflight failed',
    '',
    'Caused by:',
    '    preflight: server reported no prefix-cache hit (prompt tokens Some(8192), cached tokens Some(0)). The route reports cached tokens only behind --enable-prompt-tokens-details; see the README.',
  ].join('\n');
  return fixture({
    name: 'input-gate-failed',
    description:
      'A live run whose input benchmark failed before the first idea and never passed, so the failure stands above the figures.',
    runId: '20261019-111041-5a9c0cd3-qwen35-mi210-20261019-111041',
    state: 'live',
    build: trace => {
      trace.event('gate_finished', {gate: 'benchmark', recipe: null, output_tail: tail}, 5, {
        status: 'failed',
      });
      plan(trace, 1, 10);
      addIdea(trace, {
        n: 1,
        title: 'Preallocate the arena',
        at: 100,
        round: 1,
        baseRound: null,
        end: {kind: 'passed', value: 350.39, sample: 352.1, ratio: 350.39 / 286.96, noInput: true},
        effect: 'better',
        usage: usage(47_100_000, 44_600_000, 760_700),
      });
      addIdea(trace, {
        n: 2,
        title: 'Pin the consumers to cores',
        at: 800,
        on: 1,
        round: 2,
        baseRound: 1,
        end: {kind: 'passed', value: 385.4, sample: 395.4, ratio: 385.4 / 350.4, noInput: true},
        effect: 'better',
      });
    },
  });
}

/**
 * A live run with no reading of its input and two comparison baselines it read when it started: one
 * engine measured (three samples) and one not measured yet. The headline reads the ideas against
 * the measured one, the chart draws its reference line, and the Ideas table has a ratio column for
 * each.
 */
function comparisonBaselines(): RunFixture {
  return fixture({
    name: 'comparison-baselines',
    description:
      'A live run with no input reading and two comparison baselines: one measured, one not measured.',
    runId: '20261019-120000',
    state: 'live',
    build: trace => {
      trace.event(
        'comparisons_read',
        {
          comparisons: [
            {
              name: 'vllm',
              label: 'vLLM',
              status: 'measured',
              metric: OBJECTIVE.metric,
              unit: OBJECTIVE.unit,
              direction: OBJECTIVE.direction,
              value: 66.2,
              spread: 0.02,
              samples: 3,
              hosts: ['host-a'],
            },
            {name: 'sglang', label: 'SGLang', status: 'not_measured'},
          ],
        },
        3,
      );
      plan(trace, 1, 10);
      addIdea(trace, {
        n: 1,
        title: 'Preallocate the arena',
        at: 100,
        round: 1,
        baseRound: null,
        end: {kind: 'passed', value: 350.39, sample: 352.1, ratio: 350.39 / 286.96, noInput: true},
        effect: 'better',
      });
      addIdea(trace, {
        n: 2,
        title: 'Pin the consumers to cores',
        at: 800,
        on: 1,
        round: 2,
        baseRound: 1,
        end: {kind: 'passed', value: 385.4, sample: 395.4, ratio: 385.4 / 350.4, noInput: true},
        effect: 'better',
      });
    },
  });
}

/** Ideas that build on other ideas: chains, a merge of two, a base that itself failed. */
function nonInputBases(): RunFixture {
  return fixture({
    name: 'non-input-bases',
    description:
      'Ideas whose base is another idea: a chain, a merge of two, and a base that failed.',
    runId: '20261015-200000',
    state: 'live',
    build: trace => {
      plan(trace, 1, 10);
      addIdea(trace, {
        n: 1,
        title: 'Padded counters',
        at: 100,
        end: {kind: 'passed', value: 1.2e7, ratio: 1.2},
      });
      addIdea(trace, {
        n: 2,
        title: 'Batched enqueue on the padded counters',
        at: 200,
        on: 1,
        end: {kind: 'passed', value: 1.5e7, ratio: 1.5},
      });
      addIdea(trace, {
        n: 3,
        title: 'Park idle consumers after the batching',
        at: 300,
        on: 2,
        end: {kind: 'slower', value: 1.4e7, ratio: 1.4},
      });
      addIdea(trace, {
        n: 4,
        title: 'Continue from the slower idea anyway',
        at: 400,
        on: 3,
        end: {kind: 'passed', value: 1.9e7, ratio: 1.9},
      });
      addIdea(trace, {
        n: 5,
        title: 'Independent: per-core queues',
        at: 410,
        end: {kind: 'passed', value: 1.3e7, ratio: 1.3},
      });
      addIdea(trace, {
        n: 6,
        title: 'Merge: batching with per-core queues',
        at: 700,
        on: 5,
        end: {kind: 'running'},
      });
      addIdea(trace, {
        n: 7,
        title: 'Build on a failed base',
        at: 710,
        on: 3,
        end: {kind: 'failed', reason: 'review-rejected'},
      });
      trace.span('span_started', adoptionSpan({n: 4, ratio: 1.9, value: 1.9e7}, 900, false), 900);
    },
  });
}

/** A live run in which the first batch holds part of the run and the stream drops mid-way. */
function liveReconnect(): RunFixture {
  return fixture({
    name: 'live-reconnect',
    description:
      'A live run mid-stream: events arrive after the first batch, the stream drops, and the rest follow a resubscribe.',
    runId: '20261015-210000',
    state: 'live',
    build: trace => {
      plan(trace, 1, 10);
      const ends: IdeaSpec['end'][] = [
        {kind: 'passed', value: 1.2e7, ratio: 1.2},
        {kind: 'slower', value: 0.9e7, ratio: 0.9},
        {kind: 'failed', reason: 'turned_down'},
        {kind: 'passed', value: 1.6e7, ratio: 1.6},
        {kind: 'failed_check', check: 'correctness', completed: 7, required: 100, unit: 'trials'},
        {kind: 'running'},
      ];
      ends.forEach((end, index) => {
        const n = index + 1;
        addIdea(trace, {
          n,
          title: `Live idea ${n}`,
          at: 100 + index * 120,
          on: index > 3 ? 4 : null,
          end,
        });
      });
      turnTranscript(trace, 'op:w6-turn', 'implementer', 900, [
        {
          channel: 'analysis',
          content: '**Reading the queue**\n\nThe tail counter is shared with the slot array.',
        },
        {
          channel: 'assistant',
          content: 'I will move the tail onto its own cache line and re-measure.',
        },
      ]);
      trace.span('span_started', adoptionSpan({n: 4, ratio: 1.6, value: 1.6e7}, 950, false), 950);
    },
    // The first batch ends after the third idea; the stream drops after the fifth.
    liveAfter: trace => sequenceOf(trace, 'op:w3-turn', 'span_ended'),
    dropAfter: trace => sequenceOf(trace, 'op:w5-turn', 'span_ended'),
  });
}

function sequenceOf(trace: Trace, span: string, type: string): number {
  const index = trace.events.findIndex(
    event =>
      event['type'] === type && ((event['data'] as {span?: {id?: string}}).span?.id ?? '') === span,
  );
  if (index < 0) throw new Error(`no ${type} of ${span}`);
  return index + 1;
}

/** A run that has ended: every idea settled, the best adopted. */
function finishedRun(): RunFixture {
  return fixture({
    name: 'finished-run',
    description:
      'A finished run: two planning calls, ideas that passed, failed and built on each other, the best adopted.',
    runId: '20261014-090000',
    state: 'finished',
    build: trace => {
      plan(trace, 1, 10);
      addIdea(trace, {
        n: 1,
        title: 'Lock-free ring with per-slot sequence numbers',
        at: 100,
        end: {kind: 'passed', value: 1.9e7, ratio: 1.9},
        outcome: 'nominated',
        summary:
          'Each slot carries its own sequence number, so producers and consumers never share a counter.',
      });
      addIdea(trace, {
        n: 2,
        title: 'Cache-line padding and batched enqueue',
        at: 110,
        end: {kind: 'passed', value: 1.55e7, ratio: 1.55},
      });
      addIdea(trace, {
        n: 3,
        title: 'Spin then park idle consumers',
        at: 120,
        end: {kind: 'slower', value: 0.85e7, ratio: 0.85},
      });
      addIdea(trace, {
        n: 4,
        title: 'Larger batch size',
        at: 130,
        end: {
          kind: 'failed_check',
          check: 'correctness',
          completed: 93,
          required: 100,
          unit: 'trials',
        },
      });
      plan(trace, 2, 900);
      addIdea(trace, {
        n: 5,
        title: 'Combine the ring with the padding',
        at: 960,
        on: 1,
        end: {kind: 'passed', value: 2.4e7, ratio: 2.4},
      });
      turnTranscript(trace, 'op:w5-turn', 'implementer', 1200, [
        {channel: 'analysis', content: 'The head and tail counters share a cache line. '},
        {channel: 'assistant', content: 'I will pad each counter to its own line and re-measure.'},
      ]);
      toolCall(trace, 'op:w5-turn', 'implementer', 1210, {
        id: 'c1',
        tool: 'Bash',
        command: 'cargo bench --bench queue',
        output: 'queue/mpmc time: 41.2 ms',
      });
      // The judge's turn of idea 5: two lines of thinking, then a card with a long analysis.
      addJudgeTurn(trace, {
        n: 5,
        at: 1322,
        took: 26,
        thinking: [
          'The candidate source matches the declared outcome.\n',
          'The benchmark and correctness gates both passed on the final revision.',
        ],
        reply: {
          verdict: 'pass',
          analysis:
            'The candidate source matches the declared outcome: the head and tail counters now sit on separate cache lines, the ring still uses per-slot sequence numbers, and the correctness gate passed all 100 trials. The padding adds 128 bytes per queue, which the benchmark shows is repaid by the removed false sharing. No test was weakened and no dependency was added.',
        },
      });
      addIdea(trace, {
        n: 6,
        title: 'Wait-free consumer path',
        at: 970,
        on: 1,
        tries: ['build failed: missing Send bound'],
        end: {kind: 'failed', reason: 'implementation-failed'},
      });
      addIdea(trace, {
        n: 7,
        title: 'Per-core queues with stealing',
        at: 980,
        on: 5,
        end: {kind: 'passed', value: 2.2e7, ratio: 2.2},
      });
      trace.span('span_started', adoptionSpan({n: 5, ratio: 2.4, value: 2.4e7}, 1700, false), 1700);
      trace.span('span_ended', adoptionSpan({n: 5, ratio: 2.4, value: 2.4e7}, 1700, true), 1701);
      trace.span('span_ended', rootSpan('done', 1710), 1710);
    },
  });
}

/** The input's measured value in the ten-idea run, in the objective's unit. */
const TEN_INPUT = 1_581_539;

/** What a turn reports: input with its cached share, and output with a quarter of it reasoning. */
function usage(input: number, cached: number, output: number): TurnUsage {
  // The cost a provider could report for the counts (uncached and cached input, output), and the
  // time the counts take at a steady rate: both are functions of the counts, so they stay in step.
  const cost = (input - cached) * 3e-6 + cached * 3e-7 + output * 1.5e-5;
  return {
    total_cost_usd: Math.round(cost * 1e4) / 1e4,
    duration_ms: Math.round(output / 4),
    input_tokens: input,
    cache_read_input_tokens: cached,
    output_tokens: output,
    reasoning_output_tokens: Math.round(output / 4),
  };
}

/** The ten ideas, each with its base (an earlier idea's number, or none for the input). */
const TEN: readonly Omit<IdeaSpec, 'at'>[] = (() => {
  const value = (ratio: number): number => Math.round(TEN_INPUT * ratio);
  const passed = (ratio: number) => ({kind: 'passed', value: value(ratio), ratio}) as const;
  return [
    {
      n: 1,
      title: 'Preallocate the arena',
      end: passed(1.31),
      usage: usage(812_000, 701_000, 41_000),
    },
    {
      n: 2,
      title: 'Isolate payload slices',
      on: 1,
      end: passed(1.8),
      usage: usage(905_000, 790_000, 52_000),
    },
    {
      n: 3,
      title: 'Replace the mutex with a ticket lock',
      on: 1,
      end: {kind: 'failed', reason: 'review-rejected'},
      usage: usage(655_000, 540_000, 30_000),
    },
    {
      n: 4,
      title: 'Separate the producer cursor',
      on: 2,
      end: passed(2.4),
      usage: usage(1_040_000, 930_000, 61_000),
    },
    {
      n: 5,
      title: 'Batch the enqueue',
      on: 4,
      end: passed(3.1),
      usage: usage(1_120_000, 1_010_000, 70_000),
    },
    {
      n: 6,
      title: 'Park idle consumers',
      on: 4,
      tries: ['build failed: missing Send bound'],
      end: {kind: 'failed', reason: 'implementation-failed'},
      // The provider reported no cache counts for this turn.
      usage: {input_tokens: 720_000, output_tokens: 38_000},
    },
    {
      n: 7,
      title: 'Pad the slot headers',
      on: 5,
      end: {kind: 'passed', value: 6_054_154, ratio: 6_054_154 / TEN_INPUT},
      usage: {...usage(1_330_000, 1_190_000, 83_000), cache_creation_input_tokens: 18_000},
    },
    {
      n: 8,
      title: 'Prefetch the next slot',
      on: 5,
      end: passed(2.9),
      usage: usage(940_000, 850_000, 55_000),
    },
    {
      n: 9,
      title: 'Widen the batch to sixty-four',
      on: 5,
      end: {
        kind: 'failed_check',
        check: 'correctness',
        completed: 93,
        required: 100,
        unit: 'trials',
      },
      usage: usage(610_000, 500_000, 29_000),
    },
    // This turn reported nothing.
    {n: 10, title: 'Spin before parking', on: 7, end: {kind: 'failed', reason: 'turned_down'}},
  ];
})();

/**
 * A run that has ended with ten ideas, six passed and four failed, the best five steps from the
 * input: the shape of a real small run. Turns report their tokens, one without the cached part and
 * one with nothing at all.
 */
function tenIdeas(): RunFixture {
  const best = TEN.find(idea => idea.n === 7);
  if (best?.end.kind !== 'passed') throw new Error('the ten-idea run has no best idea');
  const adopted = {n: best.n, ratio: best.end.ratio, value: best.end.value};
  return fixture({
    name: 'finished-ten-ideas',
    description:
      'A finished run of ten ideas: six passed and four failed, the best five steps from the input, with token counts that are partly unknown.',
    runId: '20261016-120000',
    state: 'finished',
    build: trace => {
      plan(trace, 1, 10);
      for (const idea of TEN) addIdea(trace, {...idea, at: 100 + idea.n * 60});
      trace.span('span_started', adoptionSpan(adopted, 900, false), 900);
      trace.span('span_ended', adoptionSpan(adopted, 900, true), 901);
      trace.span('span_ended', rootSpan('done', 905), 905);
    },
  });
}

const TWO_INPUT = {throughput: 1_581_539, latency: 12.4} as const;

/** Two objectives: throughput (higher is better) and 99th-percentile latency (lower is better). */
const TWO_AXES: readonly Axis[] = [
  {name: 'total_ops_per_sec', direction: 'max'},
  {name: 'p99_latency', direction: 'min'},
];

const twoReadings = (throughput: number, latency: number): readonly GateReading[] => [
  {name: 'total_ops_per_sec', value: throughput, unit: 'ops/s', direction: 'max'},
  {name: 'p99_latency', value: latency, unit: 'ms', direction: 'min'},
];

/** A gate that read both objectives, and the input's, for an idea that passed (or was slower). */
function measuredBoth(
  kind: 'passed' | 'slower',
  throughput: number,
  latency: number,
): IdeaSpec['end'] {
  return {
    kind,
    value: throughput,
    ratio: throughput / TWO_INPUT.throughput,
    readings: twoReadings(throughput, latency),
    input: twoReadings(TWO_INPUT.throughput, TWO_INPUT.latency),
  };
}

/** Thirteen ideas that trade throughput against latency: some on the frontier, some dominated. */
const TWO: readonly Omit<IdeaSpec, 'at'>[] = [
  {n: 1, title: 'Preallocate the arena', end: measuredBoth('passed', 2_070_000, 11.8)},
  {n: 2, title: 'Isolate payload slices', on: 1, end: measuredBoth('passed', 2_850_000, 10.9)},
  {
    n: 3,
    title: 'Replace the mutex with a ticket lock',
    on: 1,
    end: {kind: 'failed', reason: 'review-rejected'},
  },
  {n: 4, title: 'Separate the producer cursor', on: 2, end: measuredBoth('passed', 3_800_000, 9.6)},
  {n: 5, title: 'Batch the enqueue', on: 4, end: measuredBoth('passed', 4_900_000, 14.2)},
  {
    n: 6,
    title: 'Park idle consumers',
    on: 4,
    end: {kind: 'failed', reason: 'implementation-failed'},
  },
  {n: 7, title: 'Pad the slot headers', on: 5, end: measuredBoth('passed', 6_054_154, 15.8)},
  {n: 8, title: 'Prefetch the next slot', on: 5, end: measuredBoth('passed', 4_600_000, 12.0)},
  {
    n: 9,
    title: 'Widen the batch to sixty-four',
    on: 5,
    end: {kind: 'failed_check', check: 'correctness', completed: 93, required: 100, unit: 'trials'},
  },
  {n: 10, title: 'Spin before parking', on: 7, end: {kind: 'failed', reason: 'turned_down'}},
  {n: 11, title: 'Shard the tail pointer', on: 4, end: measuredBoth('passed', 3_500_000, 9.9)},
  {n: 12, title: 'Keep a free list per core', on: 2, end: measuredBoth('passed', 3_200_000, 8.7)},
  {n: 13, title: 'Yield on contention', on: 1, end: measuredBoth('slower', 1_450_000, 13.1)},
];

/**
 * A finished run with two objectives (throughput and 99th-percentile latency): thirteen ideas, eight
 * passed, one turned down as slower and four failed. Its passed ideas trade one objective for the
 * other, so the frontier has several points and some ideas are dominated.
 */
function twoObjectives(): RunFixture {
  const best = TWO.find(idea => idea.n === 7);
  if (best?.end.kind !== 'passed') throw new Error('the two-objective run has no best idea');
  const adopted = {n: best.n, ratio: best.end.ratio, value: best.end.value};
  return fixture({
    name: 'finished-two-objectives',
    description:
      'A finished run with two objectives, throughput and p99 latency: eight ideas passed on a trade-off between them, one was slower, four failed.',
    runId: '20261017-090000',
    state: 'finished',
    objectives: TWO_AXES,
    build: trace => {
      plan(trace, 1, 10);
      for (const idea of TWO) addIdea(trace, {...idea, at: 100 + idea.n * 60});
      trace.span('span_started', adoptionSpan(adopted, 1000, false), 1000);
      trace.span('span_ended', adoptionSpan(adopted, 1000, true), 1001);
      trace.span('span_ended', rootSpan('done', 1005), 1005);
    },
  });
}

/** Every committed synthetic scenario, by name. */
export function scenarios(): readonly RunFixture[] {
  return [
    longTitles(),
    failureReasons(),
    retries(),
    continuationRounds(),
    nonInputBases(),
    basesAndEffects(),
    liveReconnect(),
    finishedRun(),
    tenIdeas(),
    twoObjectives(),
    quotaPause(),
    inputGateFailed(),
    comparisonBaselines(),
  ];
}
