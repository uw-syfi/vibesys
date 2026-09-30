/** Rounds, the kept checkpoint, and the run's status line: pure derivations from store state. */
import type {
  HypothesisEntry,
  HypothesisRound,
  PerformanceContext,
  RunEvent,
} from '@vibesys/backend-client';
import {type CoreState, hasRunEnded, roundNumberFromLabel} from '@vibesys/core-state';
import {formatValue, latestRound, latestStartedPhase, objectiveText, titleCase} from './derive.js';
import type {CommandAction} from './session.js';

type RoundState = 'running' | 'kept' | 'reverted' | 'failed';

/** A kept result. Round 0 is the baseline; `value` is null when the run recorded none. */
interface Checkpoint {
  value: number | null;
  round: number;
}

export interface RoundRow {
  round: number;
  state: RoundState;
  title: string | null;
  hypothesis: string | null;
  /** The round's measurement, judged or not. */
  value: number | null;
  /** Kept rounds only: the move against the checkpoint before it. */
  delta: number | null;
  /** The checkpoint this round was measured against. */
  before: Checkpoint;
}

export interface RunSummary {
  rows: RoundRow[];
  unit: string | null;
  baseline: number | null;
  /** The latest kept checkpoint: what the next round has to beat. */
  retained: Checkpoint;
  /** Rounds the budget has left; null once the run ended or without a budget. */
  planned: number | null;
  lowerIsBetter: boolean;
}

export interface PlanFacts {
  title: string | null;
  hypothesis: string | null;
  passCriteria: string | null;
}

type Result = Readonly<Record<string, unknown>>;
type RoundFinished = Extract<NonNullable<RunEvent['data']>, {kind?: 'round_finished'}>;

interface Fact {
  entry: HypothesisEntry;
  round: HypothesisRound | null;
}

/** The newest `agent_execution_finished` result whose round label matches `label`. */
export function finishedResult(captured: readonly RunEvent[], label: RegExp): Result | null {
  for (let index = captured.length - 1; index >= 0; index -= 1) {
    const event = captured[index];
    const data = event?.data;
    if (data?.kind === 'agent_execution_finished' && label.test(event?.round_label ?? '')) {
      return data.result ?? null;
    }
  }
  return null;
}

export function resultText(result: Result | null, key: string): string | null {
  const value = result?.[key];
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function resultNumber(result: Result | null, key: string): number | null {
  const value = result?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** What the orchestrator planned for the round: its latest plan, reprompts included. */
export function planFacts(captured: readonly RunEvent[], round: number): PlanFacts | null {
  const result = finishedResult(captured, new RegExp(`^round-${round}(?:-retry-\\d+)?-plan$`));
  if (result === null) return null;
  return {
    title: resultText(result, 'title'),
    hypothesis: resultText(result, 'hypothesis'),
    passCriteria: resultText(result, 'pass_criteria'),
  };
}

function roundFinished(captured: readonly RunEvent[], round: number): RoundFinished | null {
  for (let index = captured.length - 1; index >= 0; index -= 1) {
    const event = captured[index];
    const data = event?.data;
    if (data?.kind === 'round_finished' && roundNumberFromLabel(event?.round_label) === round) {
      return data;
    }
  }
  return null;
}

function hypothesisFacts(experiments: readonly HypothesisEntry[]): Map<number, Fact> {
  const facts = new Map<number, Fact>();
  for (const entry of experiments) {
    for (let round = entry.first_round; round <= entry.last_round; round += 1) {
      facts.set(round, {entry, round: null});
    }
    for (const round of entry.rounds ?? []) facts.set(round.round, {entry, round});
  }
  return facts;
}

function roundState(
  core: CoreState,
  round: number,
  fact: HypothesisRound | null,
  finished: RoundFinished | null,
): RoundState {
  const summary = core.rounds.find(candidate => candidate.number === round);
  const done =
    finished !== null ||
    fact !== null ||
    summary?.status === 'completed' ||
    summary?.status === 'failed';
  if (!done) {
    // A round the run ended inside never finishes.
    if (hasRunEnded(core)) return 'failed';
    // A paused run shows once, on its run row and title; the round keeps its own state.
    return 'running';
  }
  const verdict = fact?.judge_verdict ?? finished?.judge_verdict ?? null;
  if (verdict === 'fail' || fact?.candidate_disposition === 'discard') return 'reverted';
  if (summary?.status === 'failed' || fact?.passed === false) return 'failed';
  return 'kept';
}

/** The recorded value once the round finished; before that, a benchmark or the implementer's own. */
function measured(
  core: CoreState,
  captured: readonly RunEvent[],
  round: number,
  fact: HypothesisRound | null,
  finished: RoundFinished | null,
): number | null {
  const recorded = fact?.perf_metric ?? finished?.perf_metric;
  if (fact !== null || finished !== null) return typeof recorded === 'number' ? recorded : null;
  const benchmark = core.benchmarks.filter(record => record.roundNumber === round).at(-1);
  if (benchmark !== undefined) return benchmark.value;
  const report = finishedResult(captured, new RegExp(`^round-${round}-retry-\\d+-implementer$`));
  return resultNumber(report, 'perf_metric');
}

function roundRow(
  core: CoreState,
  captured: readonly RunEvent[],
  fact: Fact | undefined,
  round: number,
  before: Checkpoint,
): RoundRow {
  const finished = roundFinished(captured, round);
  const plan = planFacts(captured, round);
  const state = roundState(core, round, fact?.round ?? null, finished);
  const value = measured(core, captured, round, fact?.round ?? null, finished);
  const delta =
    state === 'kept' && value !== null && before.value !== null ? value - before.value : null;
  return {
    round,
    state,
    title: fact?.entry.title || plan?.title || null,
    hypothesis: fact?.entry.claim || plan?.hypothesis || null,
    value,
    delta,
    before,
  };
}

function recordedUnit(
  experiments: readonly HypothesisEntry[],
  captured: readonly RunEvent[],
): string | null {
  for (const entry of experiments) {
    for (const round of entry.rounds ?? []) if (round.perf_unit) return round.perf_unit;
  }
  for (const event of captured) {
    const data = event.data;
    if (data?.kind === 'round_finished' && data.perf_unit) return data.perf_unit;
  }
  return null;
}

/**
 * Every round the run started or the experiments log names, in order, each with the checkpoint it
 * was measured against. The experiments row outranks events; events stand alone on a replay.
 */
export function runSummary(
  core: CoreState,
  captured: readonly RunEvent[],
  experiments: readonly HypothesisEntry[],
  context: PerformanceContext | null,
): RunSummary {
  const facts = hypothesisFacts(experiments);
  const numbers = new Set([...facts.keys()].filter(round => round > 0));
  for (const round of core.rounds) if (round.status !== 'planned') numbers.add(round.number);
  const baseline =
    typeof context?.objective_baseline_value === 'number' ? context.objective_baseline_value : null;
  const rows: RoundRow[] = [];
  let retained: Checkpoint = {value: baseline, round: 0};
  for (const round of [...numbers].sort((left, right) => left - right)) {
    const row = roundRow(core, captured, facts.get(round), round, retained);
    if (row.state === 'kept' && row.value !== null) retained = {value: row.value, round};
    rows.push(row);
  }
  const latest = rows.at(-1)?.round ?? null;
  return {
    rows,
    unit: context?.objective_unit || recordedUnit(experiments, captured),
    baseline,
    retained,
    planned:
      hasRunEnded(core) || core.maxRounds === null || latest === null
        ? null
        : Math.max(0, core.maxRounds - latest),
    lowerIsBetter: context?.objective_direction === 'min',
  };
}

export function signed(value: number): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : '±';
  return `${sign}${formatValue(Math.abs(value))}`;
}

export interface ResultPart {
  text: string;
  tone: 'ok' | 'bad' | null;
  /** Follows the previous part after a space instead of a `·` separator. */
  joined: boolean;
}

const part = (text: string, tone: ResultPart['tone'] = null, joined = false): ResultPart => ({
  text,
  tone,
  joined,
});

function deltaTone(delta: number, lowerIsBetter: boolean): ResultPart['tone'] {
  if (delta === 0) return null;
  return (lowerIsBetter ? delta < 0 : delta > 0) ? 'ok' : 'bad';
}

/** The sticky round header's one-line result: attempted against retained, then the verdict. */
export function resultParts(
  row: RoundRow,
  unit: string | null,
  lowerIsBetter: boolean,
): ResultPart[] {
  const suffix = unit === null ? '' : ` ${unit}`;
  const label = row.before.round === 0 ? 'Baseline' : 'Retained';
  const before =
    row.before.value === null ? [] : [part(`${label} ${formatValue(row.before.value)}`)];
  if (row.state === 'running') {
    const attempt =
      row.value === null
        ? 'Not measured yet'
        : `${formatValue(row.value)} attempted, not yet judged`;
    return [part(attempt), ...before];
  }
  if (row.state === 'kept') {
    const value = part(row.value === null ? 'Not measured' : `${formatValue(row.value)}${suffix}`);
    const delta =
      row.delta === null
        ? []
        : [part(signed(row.delta), deltaTone(row.delta, lowerIsBetter), true)];
    return [part('Accepted', 'ok'), part('Kept'), value, ...delta];
  }
  const attempt = row.value === null ? 'Not measured' : `${formatValue(row.value)} attempted`;
  const verdict = part(row.state === 'failed' ? 'Failed' : 'Rejected', 'bad');
  return [verdict, part('Reverted'), part(attempt), ...before];
}

export interface StatusLine {
  text: string;
  /** A transition is in flight: the title row shows a spinner and hides Pause/Resume. */
  busy: boolean;
  paused: boolean;
  /** The kind of the agent acting in the latest round, for the Stop confirmation. */
  activeKind: string | null;
}

const ENDED: Partial<Record<CoreState['status'], string>> = {
  completed: 'Completed',
  failed: 'Failed',
  stopped: 'Stopped',
  interrupted: 'Interrupted',
};

function activity(
  kind: string,
  label: string | null,
  round: number | null,
  summary?: string,
): string {
  if (round === null) return `${titleCase(kind)}: ${summary || 'Working'}`;
  if (label?.endsWith('-pre')) return `Reviewing before round ${round}`;
  if (label?.endsWith('-plan')) return `Planning round ${round}`;
  if (kind === 'implementer') return `Implementing round ${round}`;
  if (kind === 'judge') return `Judging round ${round}`;
  return `${titleCase(kind)}, round ${round}`;
}

function pendingText(core: CoreState, sending: CommandAction | null): string | null {
  if (core.status === 'stopping' || sending === 'stop') return 'Stopping after the current call…';
  if (core.status === 'pausing') return 'Pausing after the current call…';
  if (sending === 'pause' && core.status === 'running') return 'Pausing after the current call…';
  if (sending === 'resume' && core.status === 'paused') return 'Resuming…';
  return null;
}

/** What the run is doing now: the title row's only run-level live signal. */
export function statusLine(core: CoreState, sending: CommandAction | null): StatusLine {
  const round = latestRound(core);
  const acting = latestStartedPhase(core.phases.filter(phase => phase.status === 'active'));
  const line = (text: string, busy = false, paused = false): StatusLine => ({
    text,
    busy,
    paused,
    activeKind: acting?.kind ?? null,
  });
  const ended = ENDED[core.status];
  if (ended !== undefined) return line(ended);
  const pending = pendingText(core, sending);
  if (pending !== null) return line(pending, true);
  if (core.status === 'paused') {
    if (round === null) return line('Paused', false, true);
    const done = core.rounds.find(candidate => candidate.number === round)?.status;
    const where = done === 'completed' || done === 'failed' ? 'after' : 'in';
    return line(`Paused ${where} round ${round}`, false, true);
  }
  if (core.status === 'connecting') return line('Connecting', true);
  if (core.status !== 'running') return line('Starting', true);
  if (acting === undefined) return line(round === null ? 'Running' : `Round ${round}`);
  const summary = core.activeExecutions[acting.executionId ?? '']?.activity.summary;
  return line(activity(acting.kind, acting.roundLabel, acting.roundNumber, summary));
}

export interface RetainedText {
  label: 'Retained' | 'Baseline';
  value: string;
  unit: string | null;
  change: {text: string; tone: 'ok' | 'bad' | 't2'} | null;
  hint: string;
}

/** The title row's labelled metric: the kept checkpoint, and its move against the baseline. */
export function retainedText(summary: RunSummary): RetainedText | null {
  const {retained, baseline, unit, lowerIsBetter} = summary;
  if (retained.value === null) return null;
  const kept = retained.round > 0;
  const pct =
    kept && baseline !== null && baseline !== 0
      ? Math.round((retained.value / baseline - 1) * 100)
      : null;
  const better = pct !== null && (lowerIsBetter ? pct < 0 : pct > 0);
  const unitText = unit === null ? '' : ` ${unit}`;
  const baselineText = baseline === null ? '' : ` Baseline ${formatValue(baseline)}${unitText}.`;
  const tone = pct === 0 ? 't2' : better ? 'ok' : 'bad';
  return {
    label: kept ? 'Retained' : 'Baseline',
    value: formatValue(retained.value),
    unit,
    change: pct === null ? null : {text: `${pct >= 0 ? '+' : '−'}${Math.abs(pct)}%`, tone},
    hint: kept
      ? `Kept checkpoint of this run (round ${retained.round}).${baselineText}`
      : 'Nothing kept yet: this is the baseline.',
  };
}

export interface RunTitle {
  title: string;
  /** The whole objective, for the title's hint. */
  objective: string | null;
  project: string | null;
}

const CLAUSE = /,? (?:of|without|while|by) |, /g;
const TITLE_ROOM = 40;

/**
 * A scannable run name from the objective's first sentence: no closing punctuation, and past
 * `TITLE_ROOM` characters cut at the first clause break. The title's hint keeps the whole objective.
 */
export function shortTitle(sentence: string): string {
  const bare = sentence.replace(/[.!?]+$/, '');
  if (bare.length <= TITLE_ROOM) return bare;
  // ponytail: a break in the first dozen characters would leave a stub, so those are skipped.
  const cut = [...bare.matchAll(CLAUSE)].find(match => match.index >= 12)?.index;
  return cut === undefined ? bare : bare.slice(0, cut);
}

export function runTitle(
  context: PerformanceContext | null,
  captured: readonly RunEvent[],
  runId: string | null,
): RunTitle {
  const started = captured.find(event => event.type === 'run_started');
  const input = started?.data?.kind === 'run_started' ? started.data.input : null;
  const project = input?.split('/').filter(Boolean).at(-1) ?? null;
  const objective = objectiveText(context?.objective_description);
  return {
    title: (objective === null ? null : shortTitle(objective.first)) ?? project ?? runId ?? 'Run',
    objective: objective?.full ?? null,
    project,
  };
}
