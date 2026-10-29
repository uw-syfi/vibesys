/**
 * The incremental candidate fold. Each span event costs O(1) amortized: the fold keeps, per span,
 * the workstream that owns it, and per attempt and candidate a cached view that an event only marks
 * stale. A read builds again only the stale views, plus one pass over the candidates for the list,
 * the best and the winning path, so a client reads once per batch of events, not once per event.
 *
 * The fold is mutable on purpose: an immutable fold copies its indexes on every event, which is
 * linear in the run per event and quadratic over a run of weeks.
 *
 * Amortized, not worst case: when a span becomes a workstream after spans under it started, the
 * fold walks them once to claim them; when an attempt moves to another idea, the fold touches the
 * attempts of the two ideas and the candidates whose links name it.
 */
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import type {BestCandidate, Candidate, CandidateAttempt, CandidateTree} from './types.js';
import {attempt, attributes, candidate, revisionOf, stringOrNull} from './views.js';

/** Counts the fold's work, in spans, links and views visited; tests bound it per event. */
export interface WorkMeter {
  count(units: number): void;
}

interface AttemptState {
  span: Span;
  /** Its idea's key; null for a probe, which is no candidate. */
  key: string | null;
  /** The labelled spans it owns, in the order they started once `sorted`. */
  steps: Map<string, Span>;
  sorted: boolean;
  lastStep: number;
  links: readonly string[];
  revision: string | null;
  view: CandidateAttempt | null;
}

interface GroupState {
  readonly key: string;
  /** Its attempts, in the order they started. */
  readonly attempts: string[];
  view: {candidate: Candidate} | null;
}

const SPAN_EVENTS: ReadonlySet<string> = new Set(['span_started', 'span_updated', 'span_ended']);
const NO_METER: WorkMeter = {count: () => undefined};

export class CandidateFold {
  readonly #meter: WorkMeter;
  #sequence = 0;
  readonly #spans = new Map<string, Span>();
  readonly #order = new Map<string, number>();
  readonly #owner = new Map<string, string | null>();
  readonly #children = new Map<string, string[]>();
  readonly #attempts = new Map<string, AttemptState>();
  readonly #groups = new Map<string, GroupState>();
  #groupOrder: string[] = [];
  #groupsSorted = true;
  readonly #linkedFrom = new Map<string, Set<string>>();
  readonly #byRevision = new Map<string, Set<string>>();
  #adoption: Span | null = null;
  #candidates: readonly Candidate[] | null = null;
  #tree: CandidateTree | null = null;

  constructor(meter: WorkMeter = NO_METER) {
    this.#meter = meter;
  }

  /** A fold that starts from a snapshot's spans, at the snapshot's sequence. */
  static fromSnapshot(snapshot: RunSnapshot, meter?: WorkMeter): CandidateFold {
    const fold = new CandidateFold(meter);
    fold.applySpans(snapshot.spans ?? []);
    fold.#sequence = snapshot.sequence;
    return fold;
  }

  /** Folds `events` in order; one at or before the last folded sequence is already in. */
  apply(events: Iterable<RunEvent>): void {
    for (const event of events) {
      const at = event.sequence ?? 0;
      if (at <= this.#sequence) continue;
      this.#sequence = at;
      const span = spanOf(event);
      if (span !== null) this.#put(span);
    }
  }

  /** Folds whole spans, each replacing the span of its id; parents come before their children. */
  applySpans(spans: Iterable<Span>): void {
    for (const span of spans) this.#put(span);
  }

  /** The candidate tree now; the same object until a span changes. */
  tree(): CandidateTree {
    if (this.#tree !== null) return this.#tree;
    const candidates = this.#list();
    const best = this.#adoption === null ? null : this.#adopted(this.#adoption);
    const tree = {
      candidates,
      best,
      adoptionOpened: this.#adoption !== null,
      bestPath: pathTo(candidates, best?.candidate ?? null),
    };
    this.#meter.count(candidates.length);
    this.#tree = tree;
    return tree;
  }

  #put(span: Span): void {
    this.#meter.count(1);
    const previous = this.#spans.get(span.id);
    if (previous === undefined) this.#start(span);
    this.#spans.set(span.id, span);
    this.#candidates = null;
    this.#tree = null;
    if (span.label?.kind === 'workstream') {
      if (previous !== undefined && previous.label?.kind !== 'workstream') {
        this.#dropStep(span.id);
      }
      this.#attempt(span);
    } else {
      this.#step(span);
    }
    if (span.label?.kind === 'adoption') this.#adopt(span);
  }

  #start(span: Span): void {
    this.#order.set(span.id, this.#order.size);
    const parent = span.parent ?? null;
    if (parent === null) {
      this.#owner.set(span.id, null);
      return;
    }
    const siblings = this.#children.get(parent);
    if (siblings === undefined) this.#children.set(parent, [span.id]);
    else siblings.push(span.id);
    const above = this.#spans.get(parent);
    const owner = above?.label?.kind === 'workstream' ? parent : this.#owner.get(parent);
    this.#owner.set(span.id, owner ?? null);
  }

  /** A labelled span that is no workstream is a step of the workstream that owns it. */
  #step(span: Span): void {
    if (!span.label?.kind) {
      this.#dropStep(span.id);
      return;
    }
    const owner = this.#attempts.get(this.#owner.get(span.id) ?? '');
    if (owner === undefined) return;
    this.#addStep(owner, span);
  }

  #addStep(owner: AttemptState, span: Span): void {
    const at = this.#order.get(span.id) ?? 0;
    if (!owner.steps.has(span.id)) {
      if (at < owner.lastStep) owner.sorted = false;
      owner.lastStep = Math.max(owner.lastStep, at);
    }
    owner.steps.set(span.id, span);
    this.#stale(owner);
  }

  #dropStep(id: string): void {
    const owner = this.#attempts.get(this.#owner.get(id) ?? '');
    if (owner?.steps.delete(id)) this.#stale(owner);
  }

  #stale(state: AttemptState): void {
    state.view = null;
    const group = state.key === null ? undefined : this.#groups.get(state.key);
    if (group !== undefined) group.view = null;
  }

  #attempt(span: Span): void {
    let state = this.#attempts.get(span.id);
    if (state === undefined) state = this.#claim(span);
    state.span = span;
    this.#stale(state);
    const attrs = attributes(span);
    const hypothesis = stringOrNull(attrs['hypothesis_id']);
    let key: string | null = hypothesis === null ? `span:${span.id}` : `hypothesis:${hypothesis}`;
    if (attrs['probe'] === true) key = null;
    if (key !== state.key) this.#regroup(state, key);
    this.#relink(state, span);
    this.#rerevision(state, revisionOf(span));
  }

  /** A new workstream, and the spans under it that started before it was one. */
  #claim(span: Span): AttemptState {
    const state: AttemptState = {
      span,
      key: null,
      steps: new Map(),
      sorted: true,
      lastStep: -1,
      links: [],
      revision: null,
      view: null,
    };
    this.#attempts.set(span.id, state);
    const below = [...(this.#children.get(span.id) ?? [])];
    for (let child = below.pop(); child !== undefined; child = below.pop()) {
      this.#meter.count(1);
      const found = this.#spans.get(child);
      if (found === undefined || found.label?.kind === 'workstream') continue;
      this.#dropStep(child);
      this.#owner.set(child, span.id);
      if (found.label?.kind) this.#addStep(state, found);
      below.push(...(this.#children.get(child) ?? []));
    }
    return state;
  }

  #regroup(state: AttemptState, key: string | null): void {
    const id = state.span.id;
    if (state.key !== null) this.#leave(state.key, id);
    state.key = key;
    if (key !== null) this.#join(key, id);
    this.#staleLinkers(id);
  }

  #leave(key: string, id: string): void {
    const group = this.#groups.get(key);
    if (group === undefined) return;
    this.#meter.count(group.attempts.length);
    const index = group.attempts.indexOf(id);
    group.attempts.splice(index, 1);
    group.view = null;
    if (group.attempts.length === 0) {
      this.#groups.delete(key);
      this.#groupsSorted = false;
    } else if (index === 0) {
      this.#firstChanged(group);
    }
  }

  #join(key: string, id: string): void {
    const at = this.#order.get(id) ?? 0;
    const group = this.#groups.get(key);
    if (group === undefined) {
      const last = this.#groups.get(this.#groupOrder.at(-1) ?? '');
      if (last !== undefined && this.#firstOrder(last) > at) this.#groupsSorted = false;
      this.#groups.set(key, {key, attempts: [id], view: null});
      this.#groupOrder.push(key);
      return;
    }
    let index = group.attempts.length;
    while (index > 0 && (this.#order.get(group.attempts[index - 1] ?? '') ?? 0) > at) index -= 1;
    this.#meter.count(group.attempts.length - index + 1);
    group.attempts.splice(index, 0, id);
    group.view = null;
    if (index === 0) this.#firstChanged(group);
  }

  /** A candidate's first attempt moved: its id and its place among the candidates did. */
  #firstChanged(group: GroupState): void {
    this.#groupsSorted = false;
    for (const id of group.attempts) this.#staleLinkers(id);
  }

  #staleLinkers(target: string): void {
    for (const linker of this.#linkedFrom.get(target) ?? []) {
      this.#meter.count(1);
      const key = this.#attempts.get(linker)?.key ?? null;
      const group = key === null ? undefined : this.#groups.get(key);
      if (group !== undefined) group.view = null;
    }
  }

  #relink(state: AttemptState, span: Span): void {
    const links = (span.label?.links ?? []).filter(l => l.rel === 'builds_on').map(l => l.to);
    if (links.length === state.links.length && links.every((to, i) => to === state.links[i])) {
      return;
    }
    this.#meter.count(state.links.length + links.length);
    for (const to of state.links) this.#linkedFrom.get(to)?.delete(span.id);
    for (const to of links) {
      const from = this.#linkedFrom.get(to);
      if (from === undefined) this.#linkedFrom.set(to, new Set([span.id]));
      else from.add(span.id);
    }
    state.links = links;
  }

  #rerevision(state: AttemptState, revision: string | null): void {
    if (revision === state.revision) return;
    if (state.revision !== null) this.#byRevision.get(state.revision)?.delete(state.span.id);
    if (revision !== null) {
      const holders = this.#byRevision.get(revision);
      if (holders === undefined) this.#byRevision.set(revision, new Set([state.span.id]));
      else holders.add(state.span.id);
    }
    state.revision = revision;
  }

  #adopt(span: Span): void {
    const current = this.#adoption;
    const at = this.#order.get(span.id) ?? 0;
    if (current === null || at >= (this.#order.get(current.id) ?? 0)) this.#adoption = span;
  }

  #firstOrder(group: GroupState): number {
    return this.#order.get(group.attempts[0] ?? '') ?? 0;
  }

  #list(): readonly Candidate[] {
    if (this.#candidates !== null) return this.#candidates;
    if (!this.#groupsSorted) {
      const keys = this.#groupOrder.filter(key => this.#groups.has(key));
      this.#meter.count(keys.length);
      const first = (key: string) => this.#firstOrder(this.#groups.get(key) as GroupState);
      this.#groupOrder = keys.sort((a, b) => first(a) - first(b));
      this.#groupsSorted = true;
    }
    this.#candidates = this.#groupOrder.map(key => this.#view(this.#groups.get(key) as GroupState));
    return this.#candidates;
  }

  #view(group: GroupState): Candidate {
    if (group.view !== null) return group.view.candidate;
    this.#meter.count(group.attempts.length);
    const states = group.attempts.map(id => this.#attempts.get(id) as AttemptState);
    const first = states[0]?.span as Span;
    const built = candidate(
      first.id,
      stringOrNull(attributes(first)['hypothesis_id']),
      this.#parent(group, states),
      states.map(state => this.#attemptView(state)),
    );
    group.view = {candidate: built};
    return built;
  }

  #attemptView(state: AttemptState): CandidateAttempt {
    if (state.view !== null) return state.view;
    this.#meter.count(state.steps.size + 1);
    if (!state.sorted) {
      const ordered = [...state.steps.values()].sort(
        (a, b) => (this.#order.get(a.id) ?? 0) - (this.#order.get(b.id) ?? 0),
      );
      state.steps = new Map(ordered.map(entry => [entry.id, entry]));
      state.sorted = true;
    }
    state.view = attempt(state.span, [...state.steps.values()]);
    return state.view;
  }

  /**
   * The candidate the group built on: the first `builds_on` link, in attempt order, to an attempt
   * of an earlier candidate. A link to the group itself (an idea going on from its own code) or to
   * a span that is no earlier candidate's attempt is not an edge, so the tree has no cycle whatever
   * the links say.
   */
  #parent(group: GroupState, states: readonly AttemptState[]): string | null {
    const mine = this.#firstOrder(group);
    for (const state of states) {
      this.#meter.count(state.links.length);
      for (const to of state.links) {
        const key = this.#attempts.get(to)?.key ?? null;
        const target = key === null || key === group.key ? undefined : this.#groups.get(key);
        if (target !== undefined && this.#firstOrder(target) < mine)
          return target.attempts[0] ?? null;
      }
    }
    return null;
  }

  /**
   * The candidate the adoption span names, by the revision one of its attempts kept: the best so
   * far while the span has no verdict, the adopted result once it says `adopted`. The search owns
   * which candidate is best; the fold only reads it.
   */
  #adopted(adoption: Span): BestCandidate | null {
    const verdict = adoption.label?.verdict ?? null;
    if (verdict === 'not_adopted') return null;
    const adopted = verdict === 'adopted';
    const revision = stringOrNull(attributes(adoption)['candidate']);
    if (revision === null) return {candidate: null, adopted};
    let found: GroupState | null = null;
    for (const holder of this.#byRevision.get(revision) ?? []) {
      const key = this.#attempts.get(holder)?.key ?? null;
      const group = key === null ? undefined : this.#groups.get(key);
      if (group === undefined) continue;
      if (found === null || this.#firstOrder(group) < this.#firstOrder(found)) found = group;
    }
    const id = found?.attempts[0];
    return id === undefined ? null : {candidate: id, adopted};
  }
}

/** The candidates from the input to `best`, each built on the one before. */
function pathTo(candidates: readonly Candidate[], best: string | null): string[] {
  if (best === null) return [];
  const byId = new Map(candidates.map(item => [item.id, item]));
  const path: string[] = [];
  for (let at = byId.get(best); at !== undefined; at = byId.get(at.parent ?? '')) path.push(at.id);
  return path.reverse();
}

function spanOf(event: RunEvent): Span | null {
  if (!SPAN_EVENTS.has(event.type)) return null;
  const data = event.data;
  return data !== null && data !== undefined && 'span' in data ? data.span : null;
}
