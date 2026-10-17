/**
 * A run of ideas that build on each other, for the Ideas tree: a winning chain of `chain` ideas
 * (each on the one before) with side branches that build on a recent passed idea, a few failed
 * and the last few running. The same `ideas` and `chain` give the same run, event for event.
 */
import type {FakeServerSeed} from './fake-server.js';

const ROOT = 'flow:search/#1';
const TITLES = [
  'Preallocate the arena',
  'Measure the hot path',
  'Separate the producer cursor',
  'Use relaxed loads on the fast path',
  'Isolate false sharing',
  'Batch the enqueue',
  'Park idle consumers',
  'Pad the slot headers',
  'Replace the mutex with a ticket lock',
  'Prefetch the next slot',
];

interface Idea {
  readonly n: number;
  readonly base: number | null;
  readonly outcome: 'passed' | 'failed' | 'running';
  readonly ratio: number;
  readonly onChain: boolean;
}

/** A small deterministic generator (mulberry32). */
function randoms(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function outcomeOf(
  next: () => number,
  idea: {readonly n: number; readonly count: number; readonly chained: boolean},
): Idea['outcome'] {
  if (idea.n >= idea.count - 2) return 'running';
  if (idea.chained) return 'passed';
  // A small run fails every fourth idea; a long one passes about a quarter of them.
  if (idea.count <= 20) return idea.n % 4 === 3 ? 'failed' : 'passed';
  return next() < 0.25 ? 'passed' : 'failed';
}

function ideasOf(count: number, chain: number): Idea[] {
  const next = randoms(20261017);
  const onChain = new Set(
    Array.from({length: chain}, (_, k) => Math.floor((k * count) / (chain + 1))),
  );
  const ideas: Idea[] = [];
  const passed: Idea[] = [];
  let best = 1;
  for (let n = 0; n < count; n += 1) {
    const chained = onChain.has(n);
    const back = chained ? 0 : Math.floor(next() * Math.min(3, passed.length));
    const base = passed.length === 0 ? null : (passed.at(-1 - back)?.n ?? null);
    const outcome = outcomeOf(next, {n, count, chained});
    if (chained) best += 6 / chain / 2 + 0.05;
    const ratio = chained ? best : best * (0.6 + next() * 0.35);
    const idea: Idea = {n, base, outcome, ratio, onChain: chained};
    if (outcome === 'passed') passed.push(idea);
    ideas.push(idea);
  }
  return ideas;
}

function titleOf(n: number, count: number): string {
  const title = TITLES[n % TITLES.length] ?? 'Try something';
  return count <= TITLES.length ? title : `${title} ${n}`;
}

function spansOf(idea: Idea, count: number, span: number): Record<string, unknown>[] {
  const id = `flow:w${idea.n}/#1`;
  const start = Math.floor(idea.n * span);
  const done = idea.outcome !== 'running';
  const flow = {
    id,
    parent: ROOT,
    type: 'flow',
    name: 'workstream',
    status: done ? 'done' : 'running',
    started_at: start,
    ...(done ? {ended_at: start + 1_800} : {}),
    label: {
      kind: 'workstream',
      title: titleOf(idea.n, count),
      ...(done ? {verdict: idea.outcome} : {}),
      ...(idea.outcome === 'failed' ? {reason: 'turned_down'} : {}),
      links: idea.base === null ? [] : [{rel: 'builds_on', to: `flow:w${idea.base}/#1`}],
      attrs_json: JSON.stringify({
        hypothesis_id: `h${idea.n}`,
        idea: `Try: ${titleOf(idea.n, count).toLowerCase()}`,
        ...(idea.outcome === 'passed' ? {revision: `c${idea.n}`} : {}),
      }),
    },
  };
  const turn = {
    id: `flow:w${idea.n}/turn/#1`,
    parent: id,
    type: 'operation',
    name: 'agent turn',
    status: done ? 'done' : 'running',
    started_at: start + 1,
    ...(done ? {ended_at: start + 900} : {}),
    label: {
      kind: 'agent_turn',
      title: 'Implementer',
      attrs_json: JSON.stringify({role: 'implementer', model: 'claude-sonnet-5'}),
    },
  };
  if (!done) return [flow, turn];
  const gate = {
    id: `flow:w${idea.n}/gate/#1`,
    parent: id,
    type: 'flow',
    name: 'gate',
    status: 'done',
    started_at: start + 1_000,
    ended_at: start + 1_200,
    label: {
      kind: 'gate',
      title: 'Correctness, then benchmark',
      verdict: idea.outcome === 'passed' ? 'passed' : 'slower',
      attrs_json: JSON.stringify({
        stage: 'benchmark',
        metric_value: 1e7 * idea.ratio,
        vs_baseline: idea.ratio,
      }),
    },
  };
  return [flow, turn, gate];
}

const OVERVIEW = {
  objective: {
    text: 'Maximize trusted end-to-end throughput of the MPMC queue.',
    metric: 'total_ops_per_sec',
    unit: 'ops/s',
    direction: 'max',
    correctness_check: ['cargo', 'test', '--release', 'mpmc_trials'],
  },
};

/** The fake server's seed for run `runId`: `count` ideas over `days` days, a chain of `chain`. */
export function lineageRun(
  runId: string,
  options: {readonly count: number; readonly chain: number; readonly days: number},
): FakeServerSeed {
  const {count, chain, days} = options;
  const ideas = ideasOf(count, chain);
  const best = ideas.filter(idea => idea.onChain && idea.outcome === 'passed').at(-1);
  const span = (days * 86_400) / count;
  const all = [
    {id: ROOT, parent: null, type: 'flow', name: 'search', status: 'running', started_at: 0},
    {
      id: 'flow:base/#1',
      parent: ROOT,
      type: 'flow',
      name: 'gate',
      status: 'done',
      started_at: 0,
      ended_at: 50,
      label: {
        kind: 'gate',
        title: 'Baseline',
        verdict: 'passed',
        attrs_json: JSON.stringify({stage: 'benchmark', metric_value: 1e7, vs_baseline: 1}),
      },
    },
    {
      id: 'op:adopt',
      parent: ROOT,
      type: 'operation',
      name: 'adopt',
      status: 'running',
      started_at: 0,
      label: {
        kind: 'adoption',
        title: 'best',
        attrs_json: JSON.stringify({candidate: best === undefined ? null : `c${best.n}`}),
      },
    },
    ...ideas.flatMap(idea => spansOf(idea, count, span)),
  ];
  const events = all.map((each, index) => ({
    timestamp: '2026-10-15T16:30:51Z',
    type: 'span_started',
    sequence: index + 2,
    data: {kind: 'span_started', span: each},
  }));
  const last = all.length + 1;
  return {
    runId,
    batch: {
      type: 'event_batch',
      events: [{timestamp: '2026-10-15T16:30:51Z', type: 'server_ready', sequence: 1}, ...events],
      through_sequence: last,
      store_id: 'store-1',
      history_after_sequence: 0,
    },
    snapshot: {run_id: runId, sequence: last, status: 'running', spans: all, overview: OVERVIEW},
  };
}
