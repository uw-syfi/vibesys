/**
 * Fake data for the Run screen layout design (desktop redesign, run-layout): a queue-rs run on
 * chelan4 modelled on the e2e loop's real runs. Organized by what every search has: candidates,
 * what each built on, their agent turns and gates. Fixed content: no clock, no randomness.
 */
import type {ChartPoint, KeyValueItem, LogLine, StatusTone, ToolState} from '@vibesys/ui';

export type Outcome = 'kept' | 'rejected' | 'running';

export interface Step {
  readonly id: string;
  readonly kind: 'agent' | 'gate';
  /** The role (an agent) or `gate`; a terse label, not a sentence. */
  readonly role: string;
  /** The agent's own headline, or the gate's verdict in numbers. */
  readonly headline: string;
  readonly took: string;
  readonly tone: StatusTone;
  readonly state: string;
  readonly tools?: number;
}

export interface Candidate {
  readonly id: string;
  readonly parent: string | null;
  /** The planner's hypothesis: agent text. */
  readonly hypothesis: string;
  readonly outcome: Outcome;
  /** Measured value of the headline metric, or null (not measured, failed, running). */
  readonly value: number | null;
  /** Why it was rejected, in numbers or a short verdict. */
  readonly verdict: string | null;
  readonly startedAt: number;
  readonly took: string;
  readonly steps: readonly Step[];
  readonly fresh?: boolean;
  /** Optional attribute some searches report; never used to group. */
  readonly round?: number;
  /** Every try of the same idea, oldest first; the candidate's own value is the latest measured. */
  readonly attempts?: readonly Attempt[];
}

/** One try of an idea: what it ended in, and its value when it was measured. */
export interface Attempt {
  readonly n: number;
  readonly result: string;
  readonly value: number | null;
  readonly took: string;
}

export const INPUT = 2_330_000;
export const BEST = 13_318_503;

const agent = (
  id: string,
  role: string,
  headline: string,
  took: string,
  tools: number,
  running = false,
): Step => ({
  id,
  kind: 'agent',
  role,
  headline,
  took,
  tools,
  tone: running ? 'live' : 'ok',
  state: running ? 'Running' : 'Done',
});

const gate = (headline: string, took: string, tone: StatusTone, state: string): Step => ({
  id: 'gate',
  kind: 'gate',
  role: 'gate',
  headline,
  took,
  tone,
  state,
});

export const CANDIDATES: readonly Candidate[] = [
  {
    id: 'input',
    parent: null,
    hypothesis: 'your project',
    outcome: 'kept',
    value: INPUT,
    verdict: null,
    startedAt: 0,
    took: '41s',
    steps: [gate('2,330,000 ops/s · 100/100 trials', '41s', 'ok', 'Passed')],
  },
  {
    id: 'mpmc-ring-v1',
    parent: 'input',
    hypothesis: 'Use a sequence-numbered MPMC ring',
    outcome: 'kept',
    value: 9_530_114,
    verdict: null,
    startedAt: 1,
    took: '6m',
    round: 1,
    steps: [
      agent(
        'plan',
        'orchestrator',
        'Replace the mutex queue with a Vyukov-style bounded ring',
        '30s',
        4,
      ),
      agent(
        'impl-1',
        'implementer',
        'Ring with per-slot sequence numbers; 18 tests pass',
        '2m 10s',
        21,
      ),
      agent(
        'impl-2',
        'implementer',
        'Fixed capacity-one timeout with a single-slot state machine',
        '1m 30s',
        12,
      ),
      agent('impl-3', 'implementer', 'Requested official measurement', '20s', 3),
      gate('9,530,114 ops/s · 100/100 trials', '1m 02s', 'ok', 'Passed'),
      agent('judge', 'judge', 'Accept: 4.09× the input, all checks pass', '19s', 2),
    ],
  },
  {
    id: 'treiber-stack',
    parent: 'input',
    hypothesis: 'Replace the mutex with a lock-free Treiber stack',
    outcome: 'rejected',
    value: null,
    verdict: '93/100 trials',
    startedAt: 1,
    took: '4m',
    round: 1,
    steps: [
      agent(
        'plan',
        'orchestrator',
        'Try a CAS-based stack with a FIFO reversal on dequeue',
        '28s',
        3,
      ),
      agent(
        'impl-1',
        'implementer',
        'Treiber stack with batch reversal; local tests pass',
        '2m 05s',
        17,
      ),
      gate('FIFO order broken in 7/100 trials', '44s', 'err', 'Failed'),
      agent('judge', 'judge', 'Reject: violates global FIFO order', '15s', 1),
    ],
  },
  {
    id: 'mpmc-ring-padding',
    parent: 'mpmc-ring-v1',
    hypothesis: 'Pad producer and consumer counters to separate cache lines',
    outcome: 'kept',
    value: BEST,
    verdict: null,
    startedAt: 7,
    took: '3m 40s',
    round: 2,
    attempts: [
      {n: 1, result: 'build failed', value: null, took: '1m 10s'},
      {n: 2, result: 'measured', value: 9_553_000, took: '2m 05s'},
      {n: 3, result: 'measured', value: BEST, took: '3m 40s'},
    ],
    steps: [
      agent('plan', 'orchestrator', 'Pad head and tail to separate cache lines', '32s', 4),
      agent(
        'impl-1',
        'implementer',
        'Padded the counters; cargo test and 100 MPMC trials pass',
        '1m 40s',
        14,
      ),
      agent('impl-2', 'implementer', 'Requested official measurement', '22s', 3),
      gate('13,318,503 ops/s · 100/100 trials', '1m 05s', 'ok', 'Passed'),
      agent('judge', 'judge', 'Accept: 1.40× its parent, all checks pass', '19s', 2),
    ],
  },
  {
    id: 'batch-dequeue',
    parent: 'mpmc-ring-v1',
    hypothesis: 'Dequeue in batches of eight',
    outcome: 'rejected',
    value: 8_912_330,
    verdict: '0.94× parent',
    startedAt: 7,
    took: '4m 10s',
    round: 2,
    steps: [
      agent('plan', 'orchestrator', 'Amortize the consumer CAS over batches of eight', '27s', 3),
      agent(
        'impl-1',
        'implementer',
        'Batched dequeue behind the same C ABI; tests pass',
        '2m 20s',
        19,
      ),
      gate('8,912,330 ops/s · 100/100 trials', '1m 01s', 'ok', 'Passed'),
      agent('judge', 'judge', 'Reject: slower than mpmc-ring-v1', '17s', 1),
    ],
  },
  {
    id: 'spin-backoff',
    parent: 'mpmc-ring-padding',
    hypothesis: 'Back off exponentially on a contended CAS',
    outcome: 'running',
    value: null,
    verdict: null,
    startedAt: 11,
    took: '2m 10s',
    round: 3,
    fresh: true,
    steps: [
      agent('plan', 'orchestrator', 'Add bounded exponential backoff to both CAS loops', '25s', 3),
      agent(
        'impl-1',
        'implementer',
        'Backoff with spin hint, capped at 64 iterations; tests pass',
        '1m 15s',
        11,
      ),
      gate('2/3 repetitions · 14.1M, 14.3M ops/s', '1m 05s', 'live', 'Running'),
    ],
  },
  {
    id: 'slot-arena',
    parent: 'mpmc-ring-padding',
    hypothesis: 'Preallocate value slots in one arena',
    outcome: 'running',
    value: null,
    verdict: null,
    startedAt: 11,
    took: '3m 05s',
    round: 3,
    steps: [
      agent('plan', 'orchestrator', 'Store values inline in one cache-aligned arena', '29s', 3),
      agent(
        'impl-1',
        'implementer',
        'Arena allocator in place; 2 tests fail on capacity one',
        '1m 50s',
        16,
      ),
      agent('impl-2', 'implementer', 'Editing src/lib.rs', '46s', 6, true),
    ],
  },
];

export const OUTCOME_DOT: Readonly<
  Record<Outcome, {readonly tone: StatusTone; readonly label: string}>
> = {
  kept: {tone: 'ok', label: 'Kept'},
  rejected: {tone: 'err', label: 'Rejected'},
  running: {tone: 'live', label: 'Running'},
};

/** Every measured candidate, minutes into the run against ops/s. */
export const POINTS: readonly ChartPoint[] = [
  {id: 'input', label: 'input · 1.00×', x: 0, y: INPUT, filled: true},
  {id: 'mpmc-ring-v1', label: 'mpmc-ring-v1 · 4.09×', x: 6, y: 9_530_114, filled: true},
  {id: 'batch-dequeue', label: 'batch-dequeue · 3.82×', x: 10, y: 8_912_330, filled: false},
  {id: 'mpmc-ring-padding', label: 'mpmc-ring-padding · 5.72×', x: 11, y: BEST, filled: true},
];

export const STEPS_LINE: readonly {readonly x: number; readonly y: number}[] = [
  {x: 0, y: INPUT},
  {x: 6, y: 9_530_114},
  {x: 11, y: BEST},
  {x: 14, y: BEST},
];

/** Ratio to the input, worded as a figure. */
export function ratio(value: number): string {
  return `${(value / INPUT).toFixed(2)}×`;
}

/** One entry of an agent turn's transcript, one line first. */
export interface Entry {
  readonly id: string;
  readonly kind: 'tool' | 'reply' | 'message';
  readonly name: string;
  readonly line: string;
  readonly state: ToolState;
  readonly meta?: string;
  readonly input?: string;
  readonly output?: string;
}

/** The transcript of mpmc-ring-padding's implementer turn 1. */
export const TURN_ENTRIES: readonly Entry[] = [
  {id: 't1', kind: 'tool', name: 'read', line: 'src/lib.rs', state: 'ok', meta: '1–180'},
  {
    id: 't2',
    kind: 'tool',
    name: 'run',
    line: "grep -n 'AtomicUsize' src/lib.rs",
    state: 'ok',
    meta: '6 matches',
  },
  {
    id: 'm1',
    kind: 'message',
    name: 'says',
    line: 'Head and tail share a cache line; padding them to 128 bytes should stop the false sharing.',
    state: 'ok',
  },
  {id: 't3', kind: 'tool', name: 'edit', line: 'src/lib.rs', state: 'ok', meta: '+38 −12'},
  {
    id: 't4',
    kind: 'tool',
    name: 'run',
    line: 'cargo test --locked',
    state: 'ok',
    meta: '18 passed · 12s',
    input: '/bin/bash -lc "cargo test --locked"',
    output: [
      '   Compiling queue-rs v0.1.0 (/workspace)',
      '    Finished `test` profile [unoptimized + debuginfo] target(s) in 9.84s',
      '     Running unittests src/lib.rs (target/debug/deps/queue_rs-3f2a9c)',
      '',
      'running 18 tests',
      'test ring::tests::capacity_one ... ok',
      'test ring::tests::fifo_single_producer ... ok',
      'test ring::tests::full_observes_reserved ... ok',
      'test ring::tests::empty_observes_published ... ok',
      'test ring::tests::mpmc_smoke ... ok',
      '...',
      '',
      'test result: ok. 18 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.92s',
    ].join('\n'),
  },
  {id: 't5', kind: 'tool', name: 'run', line: 'make queue-candidate.so', state: 'ok', meta: '21s'},
  {
    id: 't6',
    kind: 'tool',
    name: 'run',
    line: 'go run . check --scenario mpmc --trials 100',
    state: 'ok',
    meta: '100/100 · 38s',
  },
  {
    id: 't7',
    kind: 'tool',
    name: 'run',
    line: 'go run . benchmark --scenario mpmc --repetitions 1',
    state: 'failed',
    meta: 'exit 2 · 41s',
  },
  {
    id: 't8',
    kind: 'tool',
    name: 'mcp',
    line: 'vs_evaluation.request',
    state: 'ok',
    meta: 'vs-20906b77',
  },
  {id: 'r1', kind: 'reply', name: 'reply', line: 'waiting_for_evaluation', state: 'ok'},
];

/** What the implementer's reply said, as facts. */
export const REPLY_FACTS: readonly KeyValueItem[] = [
  {key: 'result', value: 'Waiting for evaluation'},
  {
    key: 'summary',
    value:
      'Padded head and tail to separate cache lines (CachePadded, 128 bytes). cargo test and all 100 MPMC trials pass locally; asked for the official benchmark.',
  },
  {key: 'changed', value: 'src/lib.rs: +38 −12', mono: true},
  {key: 'handle', value: 'vs-20906b77', mono: true},
];

export function gateLog(candidate: Candidate): readonly LogLine[] {
  const running = candidate.outcome === 'running';
  const reps = running
    ? ['14102231', '14298554']
    : ['13201877', `${candidate.value ?? 0}`, '13455120'];
  return [
    {text: '$ make queue-candidate.so'},
    {text: '    Finished `release` profile [optimized] target(s) in 18.02s'},
    {text: '$ go run . check --scenario mpmc --operations 24 --trials 100'},
    {text: 'accuracy: 100/100 trials passed'},
    {text: '$ go run . benchmark --scenario mpmc --repetitions 3'},
    ...reps.map((rep, index) => ({text: `repetition ${index + 1}: total_ops_per_sec=${rep}`})),
    {text: 'warning: cpu frequency scaling is enabled; results may vary', stream: 'err' as const},
    ...(running ? [] : [{text: `median total_ops_per_sec=${candidate.value ?? 0} (spread 1.9%)`}]),
  ];
}

export const NARRATION: readonly LogLine[] = [
  {text: '21:17:36  started on chelan4 · codex gpt-6-luna · 1 h'},
  {text: '21:17:41  input measured: 2,330,000 ops/s'},
  {text: '21:18:20  planned: mpmc-ring-v1, treiber-stack'},
  {text: '21:23:52  treiber-stack rejected: FIFO order broken in 7/100 trials'},
  {text: '21:24:10  mpmc-ring-v1 kept: 9,530,114 ops/s (4.09×)'},
  {text: '21:24:40  planned: mpmc-ring-padding, batch-dequeue'},
  {text: '21:27:05  batch-dequeue rejected: 0.94× its parent'},
  {text: '21:28:31  mpmc-ring-padding kept: 13,318,503 ops/s (5.72×)'},
  {text: '21:28:58  planned: spin-backoff, slot-arena'},
];
