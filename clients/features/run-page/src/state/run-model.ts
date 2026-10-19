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
import {AgentFold, type AgentGraph, agentGraph} from '@vibesys/run-state/agents';
import {CandidateFold, type CandidateTree, candidateTree} from '@vibesys/run-state/candidates';
import {closeSpans, type RunEnding, type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import type {AppendLog} from './append-log.js';
import type {Direction} from './direction.js';
import {failuresAt, type GateFailure} from './input-gates.js';
import {overviewAt, type TraceLog, traceAt} from './live.js';
import {withLiveBest} from './live-best.js';
import {latestTime} from './span-reading.js';

/** Counts the units of work a read does (spans folded, nodes built); tests bound it. */
export interface ReadMeter {
  count(units: number): void;
}

export type {Direction};

/** The run at one sequence. */
export interface RunModel {
  /** The sequence shown; null at the live end. */
  readonly at: number | null;
  readonly trace: RunTrace;
  readonly overview: RunOverview | null;
  readonly direction: Direction;
  readonly candidates: CandidateTree;
  readonly agents: AgentGraph;
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
  read(log: TraceLog, at: number | null, ending?: RunEnding | null): RunModel;
}

const NO_METER: ReadMeter = {count: () => undefined};

function sameEnding(a: RunEnding, b: RunEnding): boolean {
  return (
    a.status === b.status && a.reason === b.reason && a.heardAt === b.heardAt && a.turns === b.turns
  );
}

/** `model` with the spans its run left open ended as `ending` says. */
function closedModel(model: RunModel, ending: RunEnding): RunModel {
  const trace = closeSpans(model.trace, ending);
  if (trace === model.trace) return model;
  const spans = traceSpans(trace);
  return {
    ...model,
    trace,
    candidates: withLiveBest(candidateTree(spans), model.direction),
    agents: agentGraph(spans),
    latest: latestTime(spans),
  };
}

export function createRunReader(meter: ReadMeter = NO_METER): RunReader {
  const live = new LiveFollower(meter);
  let past: {key: PastKey; model: RunModel} | null = null;
  let closed: {source: RunModel; ending: RunEnding; model: RunModel} | null = null;
  return {
    read(log, at, ending = null) {
      if (at === null || at >= lastSequence(log)) {
        const model = live.read(log);
        if (ending === null) return model;
        if (closed?.source !== model || !sameEnding(closed.ending, ending)) {
          meter.count(model.trace.spans.size);
          closed = {source: model, ending, model: closedModel(model, ending)};
        }
        return closed.model;
      }
      const key = pastKey(log, at);
      if (past !== null && samePastKey(past.key, key)) return past.model;
      past = {key, model: foldedAt(log, at, meter)};
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

  constructor(meter: ReadMeter) {
    this.#meter = meter;
    this.#candidates = new CandidateFold(meter);
    this.#agents = new AgentFold(meter);
  }

  read(log: TraceLog): RunModel {
    if (log === this.#log && this.#model !== null) return this.#model;
    const restarted = this.#follow(log);
    const overview = log.headOverview;
    const direction = directionOf(overview);
    const candidates = withLiveBest(this.#candidates.tree(), direction);
    const agents = this.#agents.graph();
    const previous = restarted ? null : this.#model;
    this.#log = log;
    if (
      previous !== null &&
      previous.trace === log.head &&
      previous.overview === overview &&
      previous.candidates === candidates &&
      previous.agents === agents &&
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
}

function pastKey(log: TraceLog, at: number): PastKey {
  return {base: log.base, at, event: lastEventAt(log.events, at)};
}

function samePastKey(a: PastKey, b: PastKey): boolean {
  return a.base === b.base && a.at === b.at && a.event === b.event;
}

/** The last of `events` (in sequence order) at or before `at`, found by halving. */
function lastEventAt(events: AppendLog<RunEvent>, at: number): RunEvent | null {
  return events.lastWhile(event => (event.sequence ?? 0) <= at) ?? null;
}

function foldedAt(log: TraceLog, at: number, meter: ReadMeter): RunModel {
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
    candidates: withLiveBest(candidateTree(spans), direction),
    agents: agentGraph(spans),
    latest: latestTime(spans),
    inputGates: failuresAt(log.inputGates, at),
    changed: NOTHING_CHANGED,
  };
}
