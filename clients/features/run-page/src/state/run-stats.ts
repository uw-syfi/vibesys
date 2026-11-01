/**
 * The stats row of the dashboard: the ideas the run tried, its measurements, the time its agents
 * worked within the time it ran, and the tokens it spent. Pure over the run at the position of the
 * time bar (`model`), the run to its end and that position on it (`position`), and the run's token
 * ledger, so every figure of the row says what the run had done by the position and the figures of
 * two positions add up.
 *
 * Definitions:
 * - Ideas tried: every idea the run began; measured: those with a reading of any objective;
 *   running: those still at work.
 * - Measurements: the attempts that measured a value; failed: the attempts that did not measure one
 *   (the reason `not_measured`, a failed check, a verdict `failed_check`). A re-measured idea counts
 *   once for each attempt.
 * - Running time: `runningTimeOf(...).at`, the one sum of the chart's axis and the time bar: the run clock
 *   with every stopped stretch left out.
 * - Agent time: the time at least one agent turn was working, counted on the running time's axis, so
 *   it never exceeds the running time and two agents working at once count once.
 * - Tokens: what the turns that had ended by the position spent (`tokensAt`).
 */
import type {TokenLedger, TokenTally} from '@vibesys/run-state';
import type {Candidate} from '@vibesys/run-state/candidates';
import {compactTokens} from '@vibesys/run-state/usage';
import {impactOf} from './idea-impact.js';
import {objectiveSeriesOf} from './objective-reading.js';
import type {RunModel} from './run-model.js';
import {runningTimeOf, runningWords} from './running-time.js';
import type {RunPosition} from './time-position.js';
import {tokensAt} from './token-usage.js';
import {tokensTitle} from './token-words.js';

/** A stretch of the run clock. */
interface Interval {
  readonly from: number;
  readonly to: number;
}

export interface RunStats {
  readonly ideas: {readonly tried: number; readonly measured: number; readonly running: number};
  readonly measurements: {readonly measured: number; readonly failed: number};
  /** Seconds the agents worked; null while the run has recorded no time. */
  readonly agentSeconds: number | null;
  /** Seconds the run worked at the position; null while it has recorded no time. */
  readonly runningSeconds: number | null;
  /** What the run's turns spent; null when none reported a count. */
  readonly tokens: TokenTally | null;
}

/** The union of `intervals` as disjoint stretches in order. */
function merged(intervals: readonly Interval[]): readonly Interval[] {
  const sorted = [...intervals].sort((a, b) => a.from - b.from || a.to - b.to);
  const out: {from: number; to: number}[] = [];
  for (const each of sorted) {
    const last = out.at(-1);
    if (last !== undefined && each.from <= last.to) last.to = Math.max(last.to, each.to);
    else out.push({...each});
  }
  return out;
}

/** The stretches of `model`'s agent turns, each ending no later than the run's latest time. */
function turnIntervals(model: RunModel): readonly Interval[] {
  const latest = model.latest;
  if (latest === null) return [];
  const found: Interval[] = [];
  for (const span of model.trace.spans.values()) {
    if (span.label?.kind !== 'agent_turn') continue;
    const to = Math.min(span.ended_at ?? latest, latest);
    if (span.started_at < to) found.push({from: span.started_at, to});
  }
  return found;
}

/** Whether `attempt` ended without a measured value for a reason the span states. */
function failedToMeasure(attempt: Candidate['attempts'][number]): boolean {
  const measured = attempt.measurement;
  if (measured !== null && measured.metricValue !== null) return false;
  return (
    attempt.reason === 'not_measured' ||
    measured?.failed != null ||
    measured?.verdict === 'failed_check'
  );
}

function measurementsOf(candidates: readonly Candidate[]): RunStats['measurements'] {
  let measured = 0;
  let failed = 0;
  for (const idea of candidates) {
    for (const attempt of idea.attempts) {
      if (attempt.measurement?.metricValue != null) measured += 1;
      else if (failedToMeasure(attempt)) failed += 1;
    }
  }
  return {measured, failed};
}

/** Whether `tally` states any count. */
function reported(tally: TokenTally | null | undefined): tally is TokenTally {
  return tally !== null && tally !== undefined && (tally.input !== null || tally.output !== null);
}

/**
 * The stats of the run at `model`'s position. `position.whole` is the run to its end (it holds the
 * sessions the running time is read from, and the readings the ideas measured are counted from) and
 * `start` its start on the run clock; `ledger` is what the run's turns spent, to its end.
 */
export function runStats(
  model: RunModel,
  position: RunPosition,
  start: number,
  ledger: TokenLedger,
): RunStats {
  const {whole, scrub} = position;
  const impact = impactOf(whole, objectiveSeriesOf(whole), scrub);
  const candidates = model.candidates.candidates;
  const time = runningTimeOf(whole.sessions ?? [], start);
  const worked = merged(turnIntervals(model)).reduce(
    (total, each) => total + time.at(each.to) - time.at(each.from),
    0,
  );
  const running = model.latest === null ? null : time.at(model.latest);
  const spent = tokensAt(ledger, model.trace.spans, scrub === null);
  return {
    ideas: {
      tried: candidates.length,
      measured: candidates.filter(idea =>
        (impact.cells.get(idea.id) ?? []).some(cell => cell.value !== null),
      ).length,
      running: candidates.filter(idea => idea.outcome === 'running').length,
    },
    measurements: measurementsOf(candidates),
    agentSeconds: running === null ? null : Math.min(worked, running),
    runningSeconds: running,
    tokens: reported(spent) ? spent : null,
  };
}

/** One figure of the row: its name, the figure and its unit, and the quiet line under it. */
export interface StatTile {
  readonly id: 'ideas' | 'measurements' | 'agent-time' | 'tokens';
  readonly label: string;
  readonly figure: string;
  /** A word after the figure (`input`); null for none. */
  readonly unit: string | null;
  /** The figure is a word (`Not reported`), not a number. */
  readonly quiet: boolean;
  readonly note: string;
  /** The figure whole, for the pointer (the token counts in full); null for none. */
  readonly title: string | null;
}

const NOT_REPORTED = 'Not reported';

function tokenTile(tally: TokenTally | null): StatTile {
  if (tally === null) {
    return {
      id: 'tokens',
      label: 'Tokens',
      figure: NOT_REPORTED,
      unit: null,
      quiet: true,
      note: '',
      title: null,
    };
  }
  const count = (value: number | null): string =>
    value === null ? 'unknown' : compactTokens(value);
  return {
    id: 'tokens',
    label: 'Tokens',
    figure: count(tally.input),
    unit: 'input',
    quiet: false,
    note: [
      `${tally.cacheRead === null ? 'cached unknown' : `${compactTokens(tally.cacheRead)} cached`}`,
      `${count(tally.output)} output`,
    ].join(' · '),
    title: tokensTitle(tally),
  };
}

/** The tiles of the stats row, in order, as the screen words them. */
export function statTiles(stats: RunStats): readonly StatTile[] {
  const {ideas, measurements} = stats;
  const ideaNote = [
    `${ideas.measured} measured`,
    ...(ideas.running > 0 ? [`${ideas.running} running`] : []),
  ].join(' · ');
  return [
    {
      id: 'ideas',
      label: 'Ideas tried',
      figure: String(ideas.tried),
      unit: null,
      quiet: false,
      note: ideaNote,
      title: null,
    },
    {
      id: 'measurements',
      label: 'Measurements',
      figure: String(measurements.measured),
      unit: null,
      quiet: false,
      note: `${measurements.failed} failed`,
      title: null,
    },
    {
      id: 'agent-time',
      label: 'Agent time',
      figure: stats.agentSeconds === null ? '–' : runningWords(stats.agentSeconds),
      unit: null,
      quiet: false,
      note: stats.runningSeconds === null ? '' : `of ${runningWords(stats.runningSeconds)} running`,
      title: null,
    },
    tokenTile(stats.tokens),
  ];
}
