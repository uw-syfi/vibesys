/** The Experiments pane: the retained metric by round, each round's evidence, the design summary. */
import type {DesignRound, HypothesisEntry, RunEvent} from '@vibesys/backend-client';
import {roundNumberFromLabel} from '@vibesys/core-state';
import {formatValue} from './derive.js';
import {finishedResult, planFacts, type RoundRow, type RunSummary, resultText} from './rounds.js';

type Mark = 'kept' | 'rejected' | 'judging' | 'unmeasured';

export interface ChartPoint {
  round: number;
  x: number;
  y: number;
  mark: Mark;
  label: string;
}

export interface ChartModel {
  width: number;
  height: number;
  /** Where rounds without a value sit: the bottom row, above the axis labels. */
  floor: number;
  /** The retained checkpoint as a step line, from the baseline to the latest round. */
  path: string;
  points: ChartPoint[];
  planned: Array<{round: number; x: number}>;
  ticks: Array<{round: number; x: number}>;
  baseline: {value: string; y: number} | null;
  retained: {value: string; y: number} | null;
}

const W = 368;
const H = 136;
const PAD = {l: 6, r: 42, t: 10, b: 20};
const round2 = (value: number) => Math.round(value * 100) / 100;

function pointOf(
  row: RoundRow,
  x: number,
  y: (value: number) => number,
  floor: number,
  unit: string,
): ChartPoint[] {
  const open = row.state === 'running' || row.state === 'paused';
  if (row.value === null) {
    return open
      ? []
      : [
          {
            round: row.round,
            x,
            y: floor,
            mark: 'unmeasured',
            label: `Round ${row.round}: not measured`,
          },
        ];
  }
  const mark: Mark = open ? 'judging' : row.state === 'kept' ? 'kept' : 'rejected';
  const label = `Round ${row.round}: ${formatValue(row.value)}${unit}${open ? ', not yet judged' : ''}`;
  return [{round: row.round, x, y: y(row.value), mark, label}];
}

export function chartModel(summary: RunSummary, maxRounds: number | null): ChartModel | null {
  const {rows, baseline} = summary;
  const values = rows.flatMap(row => (row.value === null ? [] : [row.value]));
  if (baseline !== null) values.push(baseline);
  if (values.length === 0) return null;
  const last = rows.at(-1)?.round ?? 0;
  const total = Math.max(maxRounds ?? 0, last, 1);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const pad = (high - low || Math.abs(high) || 1) * 0.1;
  const x = (round: number) => round2(PAD.l + ((round - 0.5) / total) * (W - PAD.l - PAD.r));
  const y = (value: number) =>
    round2(PAD.t + (1 - (value - low + pad) / (high - low + 2 * pad)) * (H - PAD.t - PAD.b));
  const floor = H - PAD.b - 5.5;
  const unit = summary.unit === null ? '' : ` ${summary.unit}`;
  const kept = rows.flatMap(row =>
    row.state === 'kept' && row.value !== null ? [{round: row.round, value: row.value}] : [],
  );
  const start = baseline ?? kept[0]?.value ?? null;
  const steps = kept.map(point => ` H${x(point.round)} V${y(point.value)}`).join('');
  return {
    width: W,
    height: H,
    floor,
    path:
      start === null ? '' : `M${PAD.l} ${y(start)}${steps} H${round2(x(Math.max(last, 1)) + 6)}`,
    points: rows.flatMap(row => pointOf(row, x(row.round), y, floor, unit)),
    planned: Array.from({length: total - last}, (_, index) => ({
      round: last + index + 1,
      x: x(last + index + 1),
    })),
    ticks: Array.from({length: total}, (_, index) => ({round: index + 1, x: x(index + 1)})),
    baseline: baseline === null ? null : {value: formatValue(baseline), y: y(baseline)},
    retained:
      summary.retained.value === null
        ? null
        : {value: formatValue(summary.retained.value), y: y(summary.retained.value)},
  };
}

export interface Evidence {
  round: number;
  title: string;
  outcome: {text: string; tone: 'ok' | 'bad' | 't2'};
  value: string | null;
  facts: Array<{term: string; text: string; mono: boolean}>;
}

const OUTCOMES: Readonly<Record<string, Evidence['outcome']>> = {
  supported: {text: 'Supported', tone: 'ok'},
  proven: {text: 'Proven', tone: 'ok'},
  nominated: {text: 'Nominated', tone: 'ok'},
  disproven: {text: 'Disproven', tone: 'bad'},
  implementation_failed: {text: 'Failed', tone: 'bad'},
  rejected: {text: 'Rejected', tone: 'bad'},
  blocked: {text: 'Blocked', tone: 'bad'},
  inconclusive: {text: 'Inconclusive', tone: 't2'},
  unmeasured: {text: 'Not measured', tone: 't2'},
};

function outcomeOf(row: RoundRow, recorded: string | undefined): Evidence['outcome'] {
  if (row.state === 'running' || row.state === 'paused') {
    return {text: row.value === null ? 'Running' : 'Judging', tone: 't2'};
  }
  const known = recorded === undefined ? undefined : OUTCOMES[recorded];
  if (known !== undefined) return known;
  if (row.state === 'kept') return {text: 'Kept', tone: 'ok'};
  return {text: row.state === 'failed' ? 'Failed' : 'Rejected', tone: 'bad'};
}

function judgeFeedback(captured: readonly RunEvent[], round: number): string | null {
  let feedback: string | null = null;
  for (const event of captured) {
    const data = event.data;
    if (data?.kind === 'judge_result' && roundNumberFromLabel(event.round_label) === round)
      feedback = data.feedback;
  }
  return feedback;
}

function implementerSummary(captured: readonly RunEvent[], round: number): string | null {
  return resultText(
    finishedResult(captured, new RegExp(`^round-${round}-retry-\\d+-implementer$`)),
    'summary',
  );
}

/** One row per round: its outcome and value, and the evidence behind them. */
export function evidenceRows(
  summary: RunSummary,
  experiments: readonly HypothesisEntry[],
  design: readonly DesignRound[],
  captured: readonly RunEvent[],
): Evidence[] {
  const outcomes = new Map<number, string>();
  for (const entry of experiments) {
    for (const round of entry.rounds ?? []) {
      if (round.hypothesis_outcome) outcomes.set(round.round, round.hypothesis_outcome);
    }
  }
  return summary.rows.map(row => {
    const changes = design.find(candidate => candidate.round === row.round);
    const facts: Array<[string, string | null, boolean]> = [
      ['Hypothesis', row.hypothesis, false],
      ['Pass criteria', planFacts(captured, row.round)?.passCriteria ?? null, false],
      ['Judge', judgeFeedback(captured, row.round), false],
      ['Change', implementerSummary(captured, row.round), false],
      ['Files', changes?.files?.map(file => file.path).join(', ') || null, true],
      ['Commit', changes?.commit ?? null, true],
    ];
    return {
      round: row.round,
      title: row.title ?? 'No hypothesis yet',
      outcome: outcomeOf(row, outcomes.get(row.round)),
      value: row.value === null ? null : formatValue(row.value),
      facts: facts.flatMap(([term, text, mono]) => (text === null ? [] : [{term, text, mono}])),
    };
  });
}

export interface DesignRow {
  round: number;
  files: string;
  summary: string | null;
  reverted: boolean;
}

/** The design summary: what each round changed, from the design query. */
export function designRows(
  summary: RunSummary,
  design: readonly DesignRound[],
  captured: readonly RunEvent[],
): DesignRow[] {
  return summary.rows.flatMap(row => {
    const files = design.find(candidate => candidate.round === row.round)?.files;
    if (files == null) return [];
    return [
      {
        round: row.round,
        files: files.map(file => file.path).join(', ') || 'no files',
        summary: implementerSummary(captured, row.round),
        reverted: row.state === 'reverted' || row.state === 'failed',
      },
    ];
  });
}
