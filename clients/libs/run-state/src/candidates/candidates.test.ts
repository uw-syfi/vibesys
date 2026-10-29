import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace, traceSpans} from '../trace/index.js';
import {
  type Candidate,
  type CandidateAttempt,
  CandidateFold,
  type CandidateTree,
  candidateChildren,
  candidateTree,
  type WorkMeter,
} from './index.js';

type SpanStatus = Span['status'];
type SpanLabel = NonNullable<Span['label']>;

/** mulberry32: a seeded generator whose low bits vary (fast-check is not a dependency). */
class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0;
  }

  int(bound: number): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  }

  pick<T>(items: readonly T[]): T {
    return items[this.int(items.length)] as T;
  }
}

const ROOT = 'flow:#1';
const ENDED: readonly SpanStatus[] = ['done', 'failed', 'stopped'];
const LIVE: readonly SpanStatus[] = ['running', 'waiting', 'pending'];

function event(sequence: number, span: Span, first: boolean): RunEvent {
  const ended = ENDED.includes(span.status);
  const type = first ? 'span_started' : ended ? 'span_ended' : 'span_updated';
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    data: {kind: type, span},
  } as RunEvent;
}

/** The events of `versions` (each span's versions, from its start), one after another. */
function sequenced(versions: readonly Span[][]): RunEvent[] {
  return eventsIn(versions.flat());
}

/** The events of span versions in the order given: a span's first version starts it. */
function eventsIn(order: readonly Span[]): RunEvent[] {
  const seen = new Set<string>();
  return order.map((span, index) => {
    const first = !seen.has(span.id);
    seen.add(span.id);
    return event(index + 1, span, first);
  });
}

/**
 * Each span's versions in a random interleaving: spans start in order (as a run starts them,
 * every parent before its children) and their later versions come at any time after.
 */
function shuffled(random: Seeded, versions: readonly Span[][]): RunEvent[] {
  const pending = versions.map(list => [...list]);
  let next = 0;
  const order: Span[] = [];
  while (pending.some(list => list.length > 0)) {
    const started = pending.slice(0, next).filter(list => list.length > 0);
    const choices = next < pending.length ? [...started, pending[next] as Span[]] : started;
    const list = random.pick(choices);
    if (list === pending[next]) next += 1;
    order.push(list.shift() as Span);
  }
  return eventsIn(order);
}

function folded(events: readonly RunEvent[]): Span[] {
  return [...traceSpans(foldTrace(EMPTY_TRACE, events))];
}

function treeOf(events: readonly RunEvent[]): CandidateTree {
  return candidateTree(folded(events));
}

const root: Span = {
  id: ROOT,
  parent: null,
  type: 'flow',
  name: 'search',
  status: 'running',
  started_at: 0,
};

function workstream(
  id: string,
  at: number,
  attrs: Record<string, unknown>,
  label: Partial<SpanLabel> = {},
  status: SpanStatus = 'running',
): Span {
  return {
    id,
    parent: ROOT,
    type: 'flow',
    name: 'workstream',
    status,
    started_at: at,
    ended_at: ENDED.includes(status) ? at + 1 : null,
    label: {kind: 'workstream', attrs_json: JSON.stringify(attrs), ...label},
  };
}

function gate(
  id: string,
  parent: string,
  at: number,
  attrs: Record<string, unknown>,
  verdict: string | null,
): Span {
  return {
    id,
    parent,
    type: 'operation',
    name: 'measure',
    status: 'done',
    started_at: at,
    ended_at: at + 0.5,
    label: {
      kind: 'gate',
      title: 'benchmark',
      verdict,
      attrs_json: JSON.stringify({stage: 'benchmark', ...attrs}),
    },
  };
}

function turn(id: string, parent: string, at: number, role: string, status: SpanStatus): Span {
  return {
    id,
    parent,
    type: 'operation',
    name: 'turn',
    status,
    started_at: at,
    ended_at: ENDED.includes(status) ? at + 0.25 : null,
    label: {kind: 'agent_turn', title: role, attrs_json: JSON.stringify({role, model: null})},
  };
}

function adoption(candidate: string | null, verdict: string | null): Span {
  return {
    id: 'op:adopt',
    parent: ROOT,
    type: 'operation',
    name: 'adopt',
    status: 'done',
    started_at: 100,
    ended_at: 100,
    label: {kind: 'adoption', verdict, attrs_json: JSON.stringify({candidate})},
  };
}

/** One generated attempt: its versions from start (unlinked) to its last state. */
function generatedAttempt(random: Seeded, n: number, targets: readonly string[]): Span[] {
  const id = `flow:w${n}/#1`;
  const hypothesis = random.int(5) === 0 ? null : `h${random.int(4)}`;
  const probe = random.int(6) === 0;
  const verdict = probe ? null : random.pick([null, 'passed', 'failed']);
  const links =
    targets.length > 0 && random.int(3) > 0 ? [{rel: 'builds_on', to: random.pick(targets)}] : [];
  const attrs = {hypothesis_id: hypothesis, idea: hypothesis && `idea ${hypothesis}`, probe};
  const revision = verdict === null ? null : `c${n}`;
  const status = random.pick([...LIVE, ...ENDED]);
  // a workstream may start unlabelled, or before it chose its idea, and be labelled later
  const late = random.int(4);
  const first = workstream(id, n, late === 1 ? {...attrs, hypothesis_id: null} : attrs);
  return [
    late === 0 ? {...first, label: null} : first,
    workstream(id, n, attrs, {links}),
    workstream(id, n, {...attrs, revision}, {links, verdict}, status),
  ];
}

/** One generated step of attempt `n`, directly under it or under an unlabelled task. */
function generatedStep(random: Seeded, n: number, s: number): Span[][] {
  const owner = `flow:w${n}/#1`;
  const task = `flow:w${n}/task${s}/#1`;
  const nested = random.int(3) === 0;
  const parent = nested ? task : owner;
  const at = n + s / 10;
  const wrapper: Span[][] = nested
    ? [[{id: task, parent: owner, type: 'flow', name: 'task', status: 'done', started_at: at}]]
    : [];
  if (random.int(2) === 0) {
    const id = `op:w${n}/turn${s}`;
    const begun = turn(id, parent, at, 'implementer', 'running');
    return [
      ...wrapper,
      [
        random.int(3) === 0 ? {...begun, label: null} : begun,
        turn(id, parent, at, 'implementer', random.pick(ENDED)),
      ],
    ];
  }
  const value = random.int(4) === 0 ? null : 1 + random.int(1000);
  const failed =
    random.int(3) === 0
      ? {check: 'correctness', completed: 93, required: 100, unit: 'trials'}
      : null;
  const verdict = random.pick([null, 'passed', 'failed_check', 'slower']);
  const attrs = {metric_value: value, vs_baseline: value && value / 100, failed};
  return [...wrapper, [gate(`op:w${n}/gate${s}`, parent, at, attrs, verdict)]];
}

/**
 * A generated run, as each span's versions: attempts on a few ideas (some probes, some without an
 * idea yet), each with steps, `builds_on` links (to earlier attempts when `honest`, else to any
 * span, itself and later ones included), verdicts, kept revisions, and maybe an adoption.
 */
function generated(random: Seeded, honest: boolean): Span[][] {
  const versions: Span[][] = [[root]];
  const attempts: string[] = [];
  const count = random.int(9);
  for (let n = 0; n < count; n += 1) {
    const id = `flow:w${n}/#1`;
    const targets = honest ? attempts : [...attempts, id, `flow:w${n + 1}/#1`, 'flow:gone/#1'];
    versions.push(generatedAttempt(random, n, targets));
    attempts.push(id);
    for (let s = 0; s < random.int(4); s += 1) versions.push(...generatedStep(random, n, s));
  }
  if (random.int(2) === 0) {
    const kept = versions
      .flatMap(list => list.slice(-1))
      .flatMap(span => attrsOf(span)['revision'] ?? []);
    const candidate = kept.length > 0 && random.int(4) > 0 ? (random.pick(kept) as string) : null;
    versions.push([adoption(candidate, random.pick([null, 'adopted', 'not_adopted']))]);
  }
  return versions;
}

function attrsOf(span: Span): Record<string, unknown> {
  return span.label?.attrs_json ? JSON.parse(span.label.attrs_json) : {};
}

function nearestWorkstream(span: Span, byId: ReadonlyMap<string, Span>): Span | null {
  let at = span.parent ? byId.get(span.parent) : undefined;
  while (at !== undefined) {
    if (at.label?.kind === 'workstream') return at;
    at = at.parent ? byId.get(at.parent) : undefined;
  }
  return null;
}

function allAttempts(tree: CandidateTree): CandidateAttempt[] {
  return tree.candidates.flatMap((item: Candidate) => item.attempts);
}

/** A parent is an earlier candidate, never itself; one idea per candidate; outcomes agree. */
function checkCandidates(spans: readonly Span[], tree: CandidateTree): void {
  const index = new Map(tree.candidates.map((item, position) => [item.id, position]));
  const byId = new Map(spans.map(span => [span.id, span]));
  for (const [position, item] of tree.candidates.entries()) {
    if (item.parent !== null) expect(index.get(item.parent)).toBeLessThan(position);
    expect(item.id).toBe(item.attempts[0]?.span ?? '');
    const ideas = new Set(
      item.attempts.map(a => attrsOf(byId.get(a.span) as Span)['hypothesis_id'] ?? null),
    );
    expect(ideas.size).toBe(1);
    if (item.hypothesis === null) expect(item.attempts).toHaveLength(1);
    const running = item.attempts.some(a => a.outcome === 'running');
    const passed = item.attempts.some(a => a.outcome === 'passed');
    expect(item.outcome).toBe(running ? 'running' : passed ? 'passed' : 'failed');
    const closed = item.attempts.every(a => a.endedAt !== null);
    expect(item.endedAt !== null).toBe(closed && !running);
    for (const entry of item.running) expect(ENDED).not.toContain(entry.status);
  }
}

/** Every attempt is in exactly one candidate, a probe in none; an attempt runs until decided. */
function checkAttempts(spans: readonly Span[], tree: CandidateTree): void {
  const placed = allAttempts(tree);
  const attempts = spans.filter(
    s => s.label?.kind === 'workstream' && attrsOf(s)['probe'] !== true,
  );
  expect(placed.map(a => a.span).sort()).toEqual(attempts.map(s => s.id).sort());
  for (const span of attempts) {
    const live = !ENDED.includes(span.status) && (span.label?.verdict ?? null) === null;
    expect(placed.find(a => a.span === span.id)?.outcome === 'running').toBe(live);
  }
}

/** Every labelled step belongs to the attempt it runs under, and to no other. */
function checkSteps(spans: readonly Span[], tree: CandidateTree): void {
  const byId = new Map(spans.map(span => [span.id, span]));
  const placed = new Set(allAttempts(tree).map(a => a.span));
  const owners = new Map(
    allAttempts(tree).flatMap(a => a.steps.map(s => [s.span, a.span] as const)),
  );
  for (const span of spans) {
    if (!span.label || span.label.kind === 'workstream') continue;
    const owner = nearestWorkstream(span, byId);
    expect(owners.get(span.id)).toBe(owner !== null && placed.has(owner.id) ? owner.id : undefined);
  }
}

/**
 * The best is the candidate the adoption span names, by the revision its attempt kept (never a
 * guess from the measurements): adopted once the span says so, else the best so far.
 */
function checkBest(spans: readonly Span[], tree: CandidateTree): void {
  const adopted = spans.filter(s => s.label?.kind === 'adoption').at(-1);
  if (adopted === undefined) {
    expect(tree.best).toBeNull();
    return;
  }
  if (adopted.label?.verdict === 'not_adopted') {
    expect(tree.best).toBeNull();
    return;
  }
  const revision = attrsOf(adopted)['candidate'];
  const done = adopted.label?.verdict === 'adopted';
  const owner = tree.candidates.find(item => item.attempts.some(a => a.revision === revision));
  if (revision === null) expect(tree.best).toEqual({candidate: null, adopted: done});
  else expect(tree.best).toEqual(owner ? {candidate: owner.id, adopted: done} : null);
}

/** The winning path runs from a candidate built on the input, parent to child, to the best. */
function checkPath(tree: CandidateTree): void {
  const byId = new Map(tree.candidates.map(item => [item.id, item]));
  const best = tree.best?.candidate ?? null;
  if (best === null) {
    expect(tree.bestPath).toEqual([]);
    return;
  }
  expect(tree.bestPath.at(-1)).toBe(best);
  expect(byId.get(tree.bestPath[0] ?? '')?.parent).toBeNull();
  for (const [index, id] of tree.bestPath.entries()) {
    if (index > 0) expect(byId.get(id)?.parent).toBe(tree.bestPath[index - 1] ?? '');
  }
}

function check(events: readonly RunEvent[]): void {
  const spans = folded(events);
  const tree = candidateTree(spans);
  checkCandidates(spans, tree);
  checkAttempts(spans, tree);
  checkSteps(spans, tree);
  checkBest(spans, tree);
  checkPath(tree);
}

/** Counts the fold's work: a Fake meter. */
class CountingMeter implements WorkMeter {
  units = 0;

  count(units: number): void {
    this.units += units;
  }
}

const LONG_IDEAS = 600;
const TURNS = 10;
const TURN_UPDATES = 11;

/** The versions of one attempt's spans: its workstream, its turns (each updated often), a gate. */
function longAttempt(idea: number, attemptAt: number, at: number): Span[][] {
  const id = `flow:i${idea}/a${attemptAt}/#1`;
  const links = idea === 0 ? [] : [{rel: 'builds_on', to: `flow:i${Math.floor(idea / 2)}/a0/#1`}];
  const kept = idea % 4 !== 3 && attemptAt === 0;
  const attrs = {hypothesis_id: `h${idea}`, idea: `idea ${idea}`};
  const decided = {...attrs, revision: kept ? `c${idea}` : null};
  const verdict = kept ? 'passed' : 'failed';
  const lists: Span[][] = [
    [workstream(id, at, attrs, {links}), workstream(id, at, decided, {links, verdict}, 'done')],
  ];
  for (let t = 0; t < TURNS; t += 1) {
    const turnId = `op:i${idea}/a${attemptAt}/turn${t}`;
    const live = Array.from({length: TURN_UPDATES + 1}, () =>
      turn(turnId, id, at + t / 100, 'implementer', 'running'),
    );
    lists.push([...live, turn(turnId, id, at + t / 100, 'implementer', 'done')]);
  }
  const measured = {metric_value: kept ? 100 + idea : 50};
  lists.push([
    gate(`op:i${idea}/a${attemptAt}/gate`, id, at + 0.5, measured, kept ? 'passed' : 'slower'),
  ]);
  return lists;
}

/** Span versions merged one from each list in turn, each list kept in order. */
function roundRobin(lists: readonly Span[][]): Span[] {
  const order: Span[] = [];
  const queues = lists.map(list => [...list]);
  for (let index = 0; queues.some(queue => queue.length > 0); index = (index + 1) % queues.length) {
    const next = queues[index]?.shift();
    if (next !== undefined) order.push(next);
  }
  return order;
}

interface LongRun {
  readonly events: RunEvent[];
  /** The idea each span works for. */
  readonly owner: ReadonlyMap<string, number>;
  /** How many spans each idea has. */
  readonly size: readonly number[];
}

/**
 * A run of weeks: 600 ideas, a third of them tried twice, each attempt with ten turns updated
 * often; eight attempts run at once, their events interleaved; every idea builds on an earlier one;
 * the run adopts the last kept idea.
 */
function longRun(): LongRun {
  const attempts: [number, number][] = [];
  for (let idea = 0; idea < LONG_IDEAS; idea += 1) {
    attempts.push([idea, 0]);
    if (idea % 3 === 0) attempts.push([idea, 1]);
  }
  const order: Span[] = [root];
  for (let start = 0; start < attempts.length; start += 8) {
    const block = attempts.slice(start, start + 8);
    const lists = block.map(([idea, at], index) => longAttempt(idea, at, start + index).flat());
    // each workstream starts before the spans under it
    order.push(...lists.map(list => list[0] as Span));
    order.push(...roundRobin(lists.map(list => list.slice(1))));
  }
  order.push(adoption('c598', 'adopted'));
  const owner = new Map<string, number>();
  const size = Array.from({length: LONG_IDEAS}, () => 0);
  for (const span of order) {
    const idea = /^(?:flow|op):i(\d+)\//.exec(span.id)?.[1];
    if (idea === undefined || owner.has(span.id)) continue;
    owner.set(span.id, Number(idea));
    size[Number(idea)] = (size[Number(idea)] ?? 0) + 1;
  }
  return {events: eventsIn(order), owner, size};
}

function spanIdOf(event: RunEvent): string {
  const data = event.data as {span?: Span} | null;
  return data?.span?.id ?? '';
}

describe('candidate tree', () => {
  it('roots every candidate at the input or an earlier candidate, after every event', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      const random = new Seeded(seed);
      const events = shuffled(random, generated(random, random.int(2) === 0));
      for (let end = 0; end <= events.length; end += 1) check(events.slice(0, end));
    }
  });

  it('folds events one batch at a time into the tree of the spans folded so far', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      const random = new Seeded(seed);
      const events = shuffled(random, generated(random, random.int(2) === 0));
      const fold = new CandidateFold();
      let end = 0;
      while (end < events.length) {
        const next = Math.min(events.length, end + 1 + random.int(4));
        fold.apply(events.slice(end, next));
        end = next;
        expect(fold.tree()).toEqual(treeOf(events.slice(0, end)));
      }
    }
  });

  it('skips the events a snapshot already holds', () => {
    for (let seed = 1; seed <= 50; seed += 1) {
      const random = new Seeded(seed);
      const events = sequenced(generated(random, true));
      const cut = random.int(events.length + 1);
      const snapshot = {sequence: cut, spans: folded(events.slice(0, cut))} as RunSnapshot;
      const fold = CandidateFold.fromSnapshot(snapshot);
      fold.apply(events);
      expect(fold.tree()).toEqual(treeOf(events));
    }
  });

  it('is the same tree whatever order the updates of the spans arrive in', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const random = new Seeded(seed);
      const run = generated(random, true);
      const inOrder = treeOf(sequenced(run));
      expect(treeOf(shuffled(random, run))).toEqual(inOrder);
      expect(treeOf(shuffled(random, run))).toEqual(inOrder);
    }
  });

  it('folds a run of weeks in work linear in its events, reading once per batch', () => {
    const {events, owner, size} = longRun();
    const meter = new CountingMeter();
    const fold = new CandidateFold(meter);
    let applied = 0;
    for (let start = 0; start < events.length; start += 500) {
      const slice = events.slice(start, start + 500);
      const before = meter.units;
      fold.apply(slice);
      applied += meter.units - before;
      const touched = new Set(slice.flatMap(e => owner.get(spanIdOf(e)) ?? []));
      const reading = meter.units;
      const tree = fold.tree();
      // a read builds again only the candidates this batch touched, plus one pass over the list
      const rebuilt = [...touched].reduce((sum, idea) => sum + 3 * (size[idea] ?? 0), 0);
      expect(meter.units - reading).toBeLessThanOrEqual(tree.candidates.length + rebuilt);
      // reading again with no event in between is the same tree, for no work
      const again = meter.units;
      expect(fold.tree()).toBe(tree);
      expect(meter.units).toBe(again);
    }
    expect(events.length).toBeGreaterThan(100_000);
    expect(applied).toBeLessThanOrEqual(2 * events.length);

    const tree = fold.tree();
    expect(tree).toEqual(treeOf(events));
    expect(tree.candidates).toHaveLength(LONG_IDEAS);
    const idOf = (idea: number) => `flow:i${idea}/a0/#1`;
    expect(tree.candidates.map(item => [item.id, item.parent])).toEqual(
      Array.from({length: LONG_IDEAS}, (_, idea) => [
        idOf(idea),
        idea === 0 ? null : idOf(Math.floor(idea / 2)),
      ]),
    );
    expect(tree.best).toEqual({candidate: idOf(598), adopted: true});
    const path: number[] = [];
    for (let idea = 598; idea > 0; idea = Math.floor(idea / 2)) path.unshift(idea);
    expect(tree.bestPath).toEqual([0, ...path].map(idOf));
  });

  it('is a chain with failed leaves for a search that plays one path', () => {
    const r1 = workstream(
      'flow:r1/#1',
      1,
      {hypothesis_id: 'h1', revision: 'c1', round: 1},
      {verdict: 'passed'},
      'done',
    );
    const r2 = workstream(
      'flow:r2/#1',
      2,
      {hypothesis_id: 'h2', round: 2},
      {verdict: 'failed', links: [{rel: 'builds_on', to: r1.id}]},
      'done',
    );
    const r3 = workstream(
      'flow:r3/#1',
      3,
      {hypothesis_id: 'h3', revision: 'c3', round: 3},
      {verdict: 'passed', links: [{rel: 'builds_on', to: r1.id}]},
      'done',
    );
    const r4 = workstream('flow:r4/#1', 4, {round: 4}, {title: 'round 4'});
    const tree = treeOf(sequenced([[root], [r1], [r2], [r3], [r4]]));

    expect(tree.candidates.map(c => [c.id, c.parent, c.outcome])).toEqual([
      [r1.id, null, 'passed'],
      [r2.id, r1.id, 'failed'],
      [r3.id, r1.id, 'passed'],
      [r4.id, null, 'running'],
    ]);
    expect(candidateChildren(tree, r1.id).map(c => c.id)).toEqual([r2.id, r3.id]);
    expect(tree.candidates[3]?.round).toBe(4);
  });

  it('shows the latest measured attempt of an idea, and its attempts inside it', () => {
    const idea = {hypothesis_id: 'h1', idea: 'fuse kernels'};
    const first = workstream('flow:a1/#1', 1, idea, {verdict: 'failed'}, 'done');
    const failed = {check: 'correctness', completed: 93, required: 100, unit: 'trials'};
    const second = workstream(
      'flow:a2/#1',
      2,
      {...idea, revision: 'c2'},
      {verdict: 'passed'},
      'done',
    );
    const tree = treeOf(
      sequenced([
        [root],
        [first],
        [gate('op:a1/gate', first.id, 1.5, {metric_value: null, failed}, 'failed_check')],
        [second],
        [turn('op:a2/turn', second.id, 2.1, 'implementer', 'done')],
        [gate('op:a2/gate', second.id, 2.5, {metric_value: 150, vs_baseline: 1.5}, 'passed')],
      ]),
    );

    expect(tree.candidates).toHaveLength(1);
    const [only] = tree.candidates as [Candidate];
    expect([only.id, only.idea, only.outcome]).toEqual([first.id, 'fuse kernels', 'passed']);
    expect(only.measurement?.metricValue).toBe(150);
    expect(only.attempts.map(a => a.measurement?.verdict)).toEqual(['failed_check', 'passed']);
    expect(only.attempts[0]?.measurement?.failed).toEqual(failed);
    expect(only.attempts[1]?.steps.map(s => [s.kind, s.role])).toEqual([
      ['agent_turn', 'implementer'],
      ['gate', null],
    ]);
  });

  it('shows what a passed idea measured when a later attempt at it failed', () => {
    const first = workstream(
      'flow:k1/#1',
      1,
      {hypothesis_id: 'h1', revision: 'c1'},
      {verdict: 'passed'},
      'done',
    );
    const slower = workstream('flow:k2/#1', 2, {hypothesis_id: 'h1'}, {verdict: 'failed'}, 'done');
    const tree = treeOf(
      sequenced([
        [root],
        [first],
        [gate('op:k1/gate', first.id, 1.5, {metric_value: 120}, 'passed')],
        [slower],
        [gate('op:k2/gate', slower.id, 2.5, {metric_value: 80}, 'slower')],
      ]),
    );

    expect(tree.candidates[0]?.measurement?.metricValue).toBe(120);
    expect(tree.candidates[0]?.outcome).toBe('passed');
  });

  it('names as best what the adoption span names: the best so far, then the adopted result', () => {
    const a = workstream(
      'flow:a/#1',
      1,
      {hypothesis_id: 'a', revision: 'ca'},
      {verdict: 'passed'},
      'done',
    );
    const b = workstream(
      'flow:b/#1',
      2,
      {hypothesis_id: 'b', revision: 'cb'},
      {verdict: 'passed'},
      'done',
    );
    const run = [
      [root],
      [a],
      [gate('op:a/gate', a.id, 1.5, {metric_value: 200}, 'passed')],
      [b],
      [gate('op:b/gate', b.id, 2.5, {metric_value: 100}, 'passed')],
    ];
    const adopting = (candidate: string | null, verdict: string | null) =>
      treeOf(sequenced([...run, [adoption(candidate, verdict)]])).best;

    // no adoption span yet: no guess from the gates, whichever idea measured best
    expect(treeOf(sequenced(run)).best).toBeNull();
    expect(adopting('ca', null)).toEqual({candidate: a.id, adopted: false});
    expect(adopting('cb', null)).toEqual({candidate: b.id, adopted: false});
    expect(adopting('cb', 'adopted')).toEqual({candidate: b.id, adopted: true});
    expect(adopting(null, null)).toEqual({candidate: null, adopted: false});
    expect(adopting(null, 'adopted')).toEqual({candidate: null, adopted: true});
    expect(adopting('cb', 'not_adopted')).toBeNull();
  });

  it('follows the adoption span as the best changes during the run', () => {
    const a = workstream(
      'flow:a/#1',
      1,
      {hypothesis_id: 'a', revision: 'ca'},
      {verdict: 'passed'},
      'done',
    );
    const b = workstream(
      'flow:b/#1',
      2,
      {hypothesis_id: 'b', revision: 'cb'},
      {verdict: 'passed'},
      'done',
    );
    const fold = new CandidateFold();
    fold.apply(sequenced([[root], [a], [b], [adoption('ca', null)]]));
    expect(fold.tree().best).toEqual({candidate: a.id, adopted: false});
    fold.apply(eventsIn([root, a, b, adoption('ca', null), adoption('cb', null)]).slice(-1));
    expect(fold.tree().best).toEqual({candidate: b.id, adopted: false});
    fold.apply(
      eventsIn([
        root,
        a,
        b,
        adoption('ca', null),
        adoption('cb', null),
        adoption('cb', 'adopted'),
      ]).slice(-1),
    );
    expect(fold.tree().best).toEqual({candidate: b.id, adopted: true});
  });

  it('reads the base round: null is the input, else the round of an earlier idea it builds on', () => {
    const r1 = workstream('flow:r1/#1', 1, {hypothesis_id: 'h1', round: 1, base_round: null});
    const r2 = workstream(
      'flow:r2/#1',
      2,
      {hypothesis_id: 'h2', round: 2, base_round: 1},
      {links: [{rel: 'builds_on', to: r1.id}]},
    );
    const r3 = workstream('flow:r3/#1', 3, {hypothesis_id: 'h3', round: 3, base_round: null});
    const tree = treeOf(sequenced([[root], [r1], [r2], [r3]]));

    expect(tree.candidates.map(c => [c.round, c.baseRound, c.parent])).toEqual([
      [1, null, null],
      [2, 1, r1.id],
      [3, null, null],
    ]);
  });

  it('says why a failed idea failed, as the span does', () => {
    const turned = workstream(
      'flow:t/#1',
      1,
      {hypothesis_id: 't', revision: 'ct'},
      {verdict: 'failed', reason: 'turned_down'},
      'done',
    );
    const passed = workstream('flow:p/#1', 2, {hypothesis_id: 'p'}, {verdict: 'passed'}, 'done');
    const tree = treeOf(sequenced([[root], [turned], [passed]]));

    expect(tree.candidates.map(c => [c.outcome, c.reason])).toEqual([
      ['failed', 'turned_down'],
      ['passed', null],
    ]);
  });

  it('leaves out a probe, which only looks', () => {
    const probe = workstream(
      'flow:p/#1',
      1,
      {hypothesis_id: 'evaluation-1', probe: true},
      {},
      'done',
    );
    const job = workstream('flow:j/#1', 2, {hypothesis_id: 'issue-1', idea: 'fix the cache'});
    const looked = turn('op:p/turn', probe.id, 1.2, 'perf', 'done');
    const tree = treeOf(sequenced([[root], [probe], [job], [looked]]));

    expect(tree.candidates.map(c => c.id)).toEqual([job.id]);
    expect(allAttempts(tree).flatMap(a => a.steps)).toEqual([]);
  });

  it('re-parents a candidate when an earlier attempt joins the idea it built on', () => {
    const early = workstream('flow:a/#1', 1, {round: 1});
    const linker = workstream(
      'flow:c/#1',
      2,
      {hypothesis_id: 'c'},
      {links: [{rel: 'builds_on', to: 'flow:b/#1'}]},
    );
    const target = workstream('flow:b/#1', 3, {hypothesis_id: 'h'});
    const joined = workstream('flow:a/#1', 1, {round: 1, hypothesis_id: 'h'});
    const fold = new CandidateFold();
    fold.apply(sequenced([[root], [early], [linker], [target]]));
    const before = fold.tree();
    fold.apply(eventsIn([root, early, linker, target, joined]).slice(-1));

    expect(before.candidates.find(c => c.id === linker.id)?.parent).toBeNull();
    expect(fold.tree().candidates.map(c => [c.id, c.parent])).toEqual([
      [early.id, null],
      [linker.id, early.id],
    ]);
  });

  it('keeps a round its identity when it chooses the idea it works on', () => {
    const begun = workstream('flow:r1/#1', 1, {round: 1}, {title: 'round 1'});
    const chosen = workstream(
      'flow:r1/#1',
      1,
      {round: 1, hypothesis_id: 'h1', idea: 'batch'},
      {title: 'batch'},
    );
    const working = turn('op:r1/turn', begun.id, 1.1, 'implementer', 'running');
    const before = treeOf(sequenced([[root], [begun], [working]]));
    const after = treeOf(sequenced([[root], [begun, chosen], [working]]));

    expect(before.candidates.map(c => c.id)).toEqual([begun.id]);
    expect(after.candidates.map(c => [c.id, c.idea])).toEqual([[begun.id, 'batch']]);
    expect(after.candidates[0]?.running.map(s => s.span)).toEqual([working.id]);
  });
});

describe('what a gate reads on every axis', () => {
  it('keeps the benchmark readings and the input readings its span carries, and drops a bad row', () => {
    const idea = workstream(
      'flow:m1/#1',
      1,
      {hypothesis_id: 'h1', revision: 'c1'},
      {verdict: 'passed'},
      'done',
    );
    const metrics = [
      {name: 'throughput', value: 120, unit: 'ops/s', direction: 'max'},
      {name: 'latency', value: 8.5, unit: null, direction: 'min'},
      {name: 'broken', value: 'x', unit: null, direction: 'min'},
      {name: 'sideways', value: 1, unit: null, direction: 'both'},
    ];
    const input_metrics = [{name: 'throughput', value: 100, unit: 'ops/s', direction: 'max'}];
    const tree = treeOf(
      sequenced([
        [root],
        [idea],
        [
          gate(
            'op:m1/gate',
            idea.id,
            1.5,
            {metric_value: 120, vs_baseline: 1.2, metrics, input_metrics},
            'passed',
          ),
        ],
      ]),
    );

    const measured = tree.candidates[0]?.measurement;
    expect(measured?.readings).toEqual([
      {name: 'throughput', value: 120, unit: 'ops/s', direction: 'max'},
      {name: 'latency', value: 8.5, unit: null, direction: 'min'},
    ]);
    expect(measured?.inputReadings).toEqual([
      {name: 'throughput', value: 100, unit: 'ops/s', direction: 'max'},
    ]);
  });

  it('reads none from a span that carries none (a run recorded before they existed)', () => {
    const idea = workstream('flow:m2/#1', 1, {hypothesis_id: 'h2'}, {verdict: 'passed'}, 'done');
    const tree = treeOf(
      sequenced([[root], [idea], [gate('op:m2/gate', idea.id, 1.5, {metric_value: 5}, 'passed')]]),
    );
    expect(tree.candidates[0]?.measurement?.readings).toEqual([]);
    expect(tree.candidates[0]?.measurement?.inputReadings).toEqual([]);
  });
});
