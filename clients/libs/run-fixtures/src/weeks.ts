/**
 * The 14-day run: 600 ideas over two weeks, a 15-long winning path to 7.35x, about 40 passed, a
 * third retried, three running at the end. Seeded, so it is the same run byte for byte; too large
 * to commit, so a consumer builds it with `weeksRun()`.
 */
import {parseFixture, type RunFixture} from './fixture.js';
import {runHeader} from './scenarios.js';
import {addIdea, adoptionSpan, type IdeaSpec, planningSpan, Trace} from './trace.js';

const DAY = 86_400;
const IDEAS = 600;
const PATH_LENGTH = 15;
const BEST = 7.35;
const BASE_METRIC = 2_330_000;

/** mulberry32: a float in [0, 1) per call. */
function seeded(seed: number): () => number {
  let state = seed >>> 0 || 1;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const VERBS = [
  'Pad',
  'Batch',
  'Park',
  'Steal',
  'Combine',
  'Shard',
  'Prefetch',
  'Unroll',
  'Pin',
  'Fuse',
];
const NOUNS = [
  'the head counter',
  'consumer wakeups',
  'enqueue reservations',
  'per-core deques',
  'the slot array',
  'the overflow list',
  'dequeue spins',
  'the sequence ring',
];
const TAILS = [
  '',
  ' with a bounded spin',
  ' on a separate cache line',
  ' behind a futex',
  ' per socket',
];
const REASONS = [
  'turned_down',
  'review-rejected',
  'not_measured',
  'implementation-failed',
  'turn-limit',
];

function titleOf(random: () => number, n: number): string {
  const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)] as T;
  const base = `${pick(VERBS)} ${pick(NOUNS)}${pick(TAILS)}`;
  // Every tenth title is long, as an agent's often are.
  return n % 10 === 0
    ? `${base}, then re-measure every contention level from one to sixty-four threads and keep the variant that wins at the highest`
    : base;
}

/** The idea a new one builds on: the last kept for one on the path, else one of the last three. */
function baseOf(random: () => number, passed: readonly number[], onPath: boolean): number | null {
  if (passed.length === 0) return null;
  const back = onPath ? 0 : Math.floor(random() * Math.min(3, passed.length));
  return passed.at(-1 - back) ?? null;
}

/** How an idea ended: still running, kept, measured slower, or failed for a reason. */
function endOf(
  random: () => number,
  idea: {running: boolean; keep: boolean; ratio: number},
): IdeaSpec['end'] {
  const value = Math.round(BASE_METRIC * idea.ratio);
  if (idea.running) return {kind: 'running'};
  if (idea.keep) return {kind: 'passed', value, ratio: idea.ratio};
  if (random() < 0.5) return {kind: 'slower', value, ratio: idea.ratio};
  return {kind: 'failed', reason: REASONS[Math.floor(random() * REASONS.length)] as string};
}

/** The ideas' specs: which pass, which fail and why, what each builds on, when each starts. */
function ideaSpecs(random: () => number): IdeaSpec[] {
  const span = (14 * DAY) / IDEAS;
  const onPath = new Set(
    Array.from({length: PATH_LENGTH}, (_, k) => Math.floor((k * IDEAS) / (PATH_LENGTH + 1))),
  );
  const passed: number[] = [];
  const specs: IdeaSpec[] = [];
  let best = 1;
  for (let n = 1; n <= IDEAS; n += 1) {
    const path = onPath.has(n - 1);
    const running = n > IDEAS - 3;
    const keep = !running && (path || random() < 40 / IDEAS);
    const on = baseOf(random, passed, path);
    const ratio = path
      ? best + (BEST - 1) / PATH_LENGTH
      : best * (0.6 + Math.floor(random() * 35) / 100);
    if (path) best = ratio;
    const tries = n % 3 === 0 ? [REASONS[n % REASONS.length] as string] : [];
    const end = endOf(random, {running, keep, ratio});
    if (keep) passed.push(n);
    specs.push({
      n,
      title: titleOf(random, n),
      at: 100 + Math.floor((n - 1) * span),
      on,
      tries,
      end,
      took: 1_800,
    });
  }
  return specs;
}

/** The 14-day, 600-idea run, live at its end. */
export function weeksRun(): RunFixture {
  const random = seeded(20261016);
  const trace = new Trace('20261001-000000');
  runHeader(trace, '20261001-000000');
  const specs = ideaSpecs(random);
  specs.forEach((spec, index) => {
    if (index % 40 === 0) {
      const call = planningSpan(index / 40 + 1, spec.at - 60);
      trace.span('span_started', {...call, status: 'running'}, call.started_at);
      trace.span('span_ended', call, call.started_at + 40);
    }
    addIdea(trace, spec);
  });
  const bestSpec = [...specs].reverse().find(spec => spec.end.kind === 'passed');
  if (bestSpec?.end.kind === 'passed') {
    trace.span(
      'span_started',
      adoptionSpan(
        {n: bestSpec.n, ratio: bestSpec.end.ratio, value: bestSpec.end.value},
        14 * DAY,
        false,
      ),
      14 * DAY,
    );
  }
  return parseFixture({
    format: 1,
    name: 'weeks-600-ideas',
    description:
      'Two weeks of running: 600 ideas, a 15-long winning path to 7.35x, a third retried, three running.',
    runId: '20261001-000000',
    state: 'live',
    objective: {
      text: 'Maximize trusted end-to-end throughput of the MPMC queue.',
      metric: 'total_ops_per_sec',
      unit: 'ops/s',
      direction: 'max',
    },
    events: trace.events,
  });
}
