/**
 * The run as the Run screen reads it at one sequence: its trace, overview, candidates (ideas) and
 * agents. A page owns one reader (`createRunReader`) for the run it shows, so reading is linear in
 * the run's events overall and constant per new event:
 *
 * - at the live end, the reader keeps the candidate and agent folds (`CandidateFold`, `AgentFold`)
 *   it built and folds only the events it has not seen, so a batch of new events costs work in
 *   proportion to the batch; a log that is not an extension of the one followed (a new snapshot,
 *   a rebootstrap) starts the folds again from its base;
 * - at a past sequence the scrubber chose, it folds the trace at that sequence once and keeps that
 *   model while later events arrive;
 * - reading the same log and sequence again returns the same model, doing no work.
 *
 * The reader is the page's cache, not its state: the store keeps the log (`TraceLog`), and every
 * model is a function of the log and the sequence.
 */

import type {RunEvent, RunOverview} from '@vibesys/contracts';
import type {LoadProgress} from '@vibesys/run-skeleton';
import type {ComparisonBaseline, CoreState} from '@vibesys/run-state';
import {AgentFold, type AgentGraph, agentGraph} from '@vibesys/run-state/agents';
import {CandidateFold, type CandidateTree, candidateTree} from '@vibesys/run-state/candidates';
import type {HeadlineReading} from '@vibesys/run-state/skeleton';
import {closeSpans, type RunEnding, type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import type {AppendLog} from './append-log.js';
import type {Direction} from './direction.js';
import {failuresAt, type GateFailure} from './input-gates.js';
import {overviewAt, type TraceLog, traceAt} from './live.js';
import {withLiveBest} from './live-best.js';
import type {PerformanceFold} from './performance.js';
import {NO_PAUSES, type QuotaPauseSpan} from './quota-pauses.js';
import {settleIdeas} from './settled-ideas.js';
import {latestTime} from './span-reading.js';

/** Counts the units of work a read does (spans folded, nodes built); tests bound it. */
export interface ReadMeter {
  count(units: number): void;
}

export type {Direction};

/**
 * What the run's core states that the trace's spans do not: the comparisons that settled each
 * idea's figures and the stretches turns were stopped on a capacity limit. A model is a function of
 * the log, the sequence and these.
 */
export interface RunFacts {
  readonly comparisons: CoreState['comparisons'];
  /** The comparison baselines the run read: other engines' readings of the task's benchmark. */
  readonly baselines: readonly ComparisonBaseline[];
  readonly pauses: readonly QuotaPauseSpan[];
}

export const NO_FACTS: RunFacts = {comparisons: {}, baselines: [], pauses: NO_PAUSES};

function sameFacts(a: RunFacts, b: RunFacts): boolean {
  return a.comparisons === b.comparisons && a.baselines === b.baselines && a.pauses === b.pauses;
}

/** The run at one sequence. */
export interface RunModel {
  /** Whether the reader has caught up with the committed prefix. */
  readonly progress?: LoadProgress | undefined;
  readonly loadFailure?: string | undefined;
  /** A bounded recovery could not prove whether an older diagnostic exists. */
  readonly diagnosticsIncomplete?: boolean | undefined;
  /**
   * The input's measured value of the objective metric, read from the input gate itself: known
   * before any idea is measured, when no idea's measurement carries it.
   */
  readonly input?: number | null;
  /** The writer's headline at exactly this committed prefix, when available. */
  readonly headline?: HeadlineReading | undefined;
  /** The writer's metric series, including measurements with no idea owner. */
  readonly series?: ReadonlyMap<string, PerformanceFold>;
  /** The sequence shown; null at the live end. */
  readonly at: number | null;
  readonly trace: RunTrace;
  readonly overview: RunOverview | null;
  readonly direction: Direction;
  readonly candidates: CandidateTree;
  readonly agents: AgentGraph;
  /** Where turns were stopped on a capacity limit: their time is not agent work. */
  readonly pauses: readonly QuotaPauseSpan[];
  /** The comparison baselines the run read (`RunFacts`); other engines' readings to set beside its own. */
  readonly baselines: readonly ComparisonBaseline[];
  /** The latest run-clock time the trace's spans carry, or null when they carry none. */
  readonly latest: number | null;
  /** The gates that failed before the first idea began: the run could not measure its input. */
  readonly inputGates: readonly GateFailure[];
  /**
   * The ideas that changed or arrived in the events this model added to the one read before it at
   * the live end (rows flash once); empty for a past sequence, a run read afresh, and the first
   * ideas a page reads (its history, not news).
   */
  readonly changed: ReadonlySet<string>;
}

const NOTHING_CHANGED: ReadonlySet<string> = new Set();

/** The ideas of `next` that are not the same objects in `previous` (the fold reuses unchanged ones). */
function changedSince(
  previous: CandidateTree,
  next: CandidateTree,
  meter: ReadMeter,
): ReadonlySet<string> {
  if (previous.candidates.length === 0) return NOTHING_CHANGED;
  meter.count(previous.candidates.length + next.candidates.length);
  const before = new Set(previous.candidates);
  return new Set(next.candidates.filter(item => !before.has(item)).map(item => item.id));
}

/** A page's reader of its run. */
export interface RunReader {
  /**
   * The run in `log` at sequence `at` (null: the live end). Once the run is over (`ending`), its
   * live end has every span the run left open ended as the ending says (`closeSpans`), so no idea,
   * step or agent reads as running and every duration stops where the run did.
   */
  read(log: TraceLog, at: number | null, ending?: RunEnding | null, facts?: RunFacts): RunModel;
}

const NO_METER: ReadMeter = {count: () => undefined};

function sameEnding(a: RunEnding, b: RunEnding): boolean {
  return (
    a.status === b.status && a.reason === b.reason && a.heardAt === b.heardAt && a.turns === b.turns
  );
}

/** `model` with the spans its run left open ended as `ending` says. */
function closedModel(model: RunModel, ending: RunEnding, facts: RunFacts): RunModel {
  const trace = closeSpans(model.trace, ending);
  if (trace === model.trace) return model;
  const spans = traceSpans(trace);
  return {
    ...model,
    trace,
    candidates: withLiveBest(settleIdeas(candidateTree(spans), facts.comparisons), model.direction),
    agents: agentGraph(spans),
    latest: latestTime(spans),
  };
}

export function createRunReader(meter: ReadMeter = NO_METER): RunReader {
  const live = new LiveFollower(meter);
  let past: {key: PastKey; model: RunModel} | null = null;
  let closed: {source: RunModel; ending: RunEnding; model: RunModel} | null = null;
  return {
    read(log, at, ending = null, facts = NO_FACTS) {
      if (at === null || at >= lastSequence(log)) {
        const model = live.read(log, facts);
        if (ending === null) return model;
        if (closed?.source !== model || !sameEnding(closed.ending, ending)) {
          meter.count(model.trace.spans.size);
          closed = {source: model, ending, model: closedModel(model, ending, facts)};
        }
        return closed.model;
      }
      const key = pastKey(log, at, facts);
      if (past !== null && samePastKey(past.key, key)) return past.model;
      past = {key, model: foldedAt(log, at, facts, meter)};
      return past.model;
    },
  };
}

function directionOf(overview: RunOverview | null): Direction {
  const direction = overview?.objective.direction;
  return direction === 'max' || direction === 'min' ? direction : null;
}

function lastSequence(log: TraceLog): number {
  return log.events.at(-1)?.sequence ?? log.base.sequence;
}

/** The live end's folds, extended by the events of each log that extends the one followed. */
class LiveFollower {
  readonly #meter: ReadMeter;
  #base: RunTrace | null = null;
  #applied = 0;
  #last: RunEvent | null = null;
  #candidates: CandidateFold;
  #agents: AgentFold;
  #model: RunModel | null = null;
  #log: TraceLog | null = null;
  #facts: RunFacts = NO_FACTS;

  constructor(meter: ReadMeter) {
    this.#meter = meter;
    this.#candidates = new CandidateFold(meter);
    this.#agents = new AgentFold(meter);
  }

  read(log: TraceLog, facts: RunFacts): RunModel {
    if (log === this.#log && this.#model !== null && sameFacts(facts, this.#facts)) {
      return this.#model;
    }
    const restarted = this.#follow(log);
    const overview = log.headOverview;
    const direction = directionOf(overview);
    const candidates = withLiveBest(
      settleIdeas(this.#candidates.tree(), facts.comparisons),
      direction,
    );
    const agents = this.#agents.graph();
    const previous = restarted ? null : this.#model;
    this.#log = log;
    this.#facts = facts;
    if (
      previous !== null &&
      previous.trace === log.head &&
      previous.overview === overview &&
      previous.candidates === candidates &&
      previous.agents === agents &&
      previous.pauses === facts.pauses &&
      previous.baselines === facts.baselines &&
      previous.latest === log.latest &&
      previous.inputGates === log.inputGates.failures
    ) {
      return previous;
    }
    const changed =
      previous === null || previous.candidates === candidates
        ? NOTHING_CHANGED
        : changedSince(previous.candidates, candidates, this.#meter);
    this.#model = {
      at: null,
      trace: log.head,
      overview,
      direction,
      candidates,
      agents,
      pauses: facts.pauses,
      baselines: facts.baselines,
      latest: log.latest,
      inputGates: log.inputGates.failures,
      changed,
    };
    return this.#model;
  }

  /**
   * Folds the events of `log` not folded yet, or starts again when `log` does not extend; says
   * whether it started again.
   */
  #follow(log: TraceLog): boolean {
    const restarted = !this.#extends(log);
    if (restarted) {
      this.#base = log.base;
      this.#applied = 0;
      this.#last = null;
      this.#candidates = new CandidateFold(this.#meter);
      this.#agents = new AgentFold(this.#meter);
      const spans = traceSpans(log.base);
      this.#candidates.applySpans(spans);
      this.#agents.applySpans(spans);
    }
    const added = log.events.slice(this.#applied);
    if (added.length === 0) return restarted;
    this.#candidates.apply(added);
    this.#agents.apply(added);
    this.#applied = log.events.length;
    this.#last = log.events.at(-1) ?? null;
    return restarted;
  }

  /** Whether `log` is the followed log with events appended (or the same). */
  #extends(log: TraceLog): boolean {
    if (log.base !== this.#base || log.events.length < this.#applied) return false;
    return this.#applied === 0 || log.events.at(this.#applied - 1) === this.#last;
  }
}

/** What a past model depends on: the base and the last event at or before the sequence. */
interface PastKey {
  readonly base: RunTrace;
  readonly at: number;
  readonly event: RunEvent | null;
  readonly facts: RunFacts;
}

function pastKey(log: TraceLog, at: number, facts: RunFacts): PastKey {
  return {base: log.base, at, event: lastEventAt(log.events, at), facts};
}

function samePastKey(a: PastKey, b: PastKey): boolean {
  return a.base === b.base && a.at === b.at && a.event === b.event && sameFacts(a.facts, b.facts);
}

/** The last of `events` (in sequence order) at or before `at`, found by halving. */
function lastEventAt(events: AppendLog<RunEvent>, at: number): RunEvent | null {
  return events.lastWhile(event => (event.sequence ?? 0) <= at) ?? null;
}

function foldedAt(log: TraceLog, at: number, facts: RunFacts, meter: ReadMeter): RunModel {
  const trace = traceAt(log, at);
  const overview = overviewAt(log, at);
  const direction = directionOf(overview);
  const spans = traceSpans(trace);
  meter.count(spans.length);
  return {
    at,
    trace,
    overview,
    direction,
    candidates: withLiveBest(settleIdeas(candidateTree(spans), facts.comparisons), direction),
    agents: agentGraph(spans),
    pauses: facts.pauses,
    baselines: facts.baselines,
    latest: latestTime(spans),
    inputGates: failuresAt(log.inputGates, at),
    changed: NOTHING_CHANGED,
  };
}
