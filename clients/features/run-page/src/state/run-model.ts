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
import {type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import {overviewAt, type TraceLog, traceAt} from './live.js';

/** Counts the units of work a read does (spans folded, nodes built); tests bound it. */
export interface ReadMeter {
  count(units: number): void;
}

/** Which way the run's metric improves; null before the run declared it. */
export type Direction = 'max' | 'min' | null;

/** The run at one sequence. */
export interface RunModel {
  /** The sequence shown; null at the live end. */
  readonly at: number | null;
  readonly trace: RunTrace;
  readonly overview: RunOverview | null;
  readonly direction: Direction;
  readonly candidates: CandidateTree;
  readonly agents: AgentGraph;
}

/** A page's reader of its run. */
export interface RunReader {
  /** The run in `log` at sequence `at` (null: the live end). */
  read(log: TraceLog, at: number | null): RunModel;
}

const NO_METER: ReadMeter = {count: () => undefined};

export function createRunReader(meter: ReadMeter = NO_METER): RunReader {
  const live = new LiveFollower(meter);
  let past: {key: PastKey; model: RunModel} | null = null;
  return {
    read(log, at) {
      if (at === null || at >= lastSequence(log)) return live.read(log);
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
    this.#follow(log);
    const overview = log.headOverview;
    const direction = directionOf(overview);
    const candidates = this.#candidates.tree(direction);
    const agents = this.#agents.graph();
    const previous = this.#model;
    this.#log = log;
    this.#model =
      previous !== null &&
      previous.trace === log.head &&
      previous.overview === overview &&
      previous.candidates === candidates &&
      previous.agents === agents
        ? previous
        : {at: null, trace: log.head, overview, direction, candidates, agents};
    return this.#model;
  }

  /** Folds the events of `log` not folded yet, or starts again when `log` does not extend. */
  #follow(log: TraceLog): void {
    if (!this.#extends(log)) {
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
    if (added.length === 0) return;
    this.#candidates.apply(added);
    this.#agents.apply(added);
    this.#applied = log.events.length;
    this.#last = log.events.at(-1) ?? null;
  }

  /** Whether `log` is the followed log with events appended (or the same). */
  #extends(log: TraceLog): boolean {
    if (log.base !== this.#base || log.events.length < this.#applied) return false;
    return this.#applied === 0 || log.events[this.#applied - 1] === this.#last;
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
function lastEventAt(events: readonly RunEvent[], at: number): RunEvent | null {
  let low = 0;
  let high = events.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if ((events[middle]?.sequence ?? 0) <= at) low = middle + 1;
    else high = middle;
  }
  return low === 0 ? null : (events[low - 1] ?? null);
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
    candidates: candidateTree(spans, direction),
    agents: agentGraph(spans),
  };
}
