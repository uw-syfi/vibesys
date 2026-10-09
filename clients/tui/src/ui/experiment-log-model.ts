import {
  detailedHypothesis,
  experimentIndexItems,
  hypothesisPlanningActivity,
  selectedExperimentIndexItem,
  unownedExperimentRounds,
} from '../experiments.js';
import type {
  ExperimentEntry,
  ExperimentIndexItem,
  ExperimentRound,
  HypothesisPlanningActivity,
  SessionState,
} from '../session-model.js';
import type {Theme} from '../theme.js';
import {displayWidth, padToWidth, truncateToWidth} from './text-width.js';

export const CLAIM_MIN_WIDTH = 90;
export const MEASURED_MIN_WIDTH = 62;
const KEPT_MIN_WIDTH = 104;
const HINT_MIN_WIDTH = 60;
export interface Columns {
  claim: boolean;
  measured: boolean;
  kept: boolean;
  claimWidth: number;
}

export type LogRenderMode =
  | {kind: 'detail'; entry: ExperimentEntry}
  | {kind: 'error'; message: string}
  | {kind: 'loading'}
  | {kind: 'kickoff'; activity: HypothesisPlanningActivity}
  | {kind: 'table'}
  | {kind: 'empty'};

type TableRowPlan =
  | {kind: 'hypothesis'; entry: ExperimentEntry; key: string; index: number; selected: boolean}
  | {kind: 'round'; roundNumber: number; index: number; selected: boolean}
  | {kind: 'activity-heading'}
  | {
      kind: 'activity';
      item: Extract<ExperimentIndexItem, {kind: 'activity'}>;
      existingHypotheses: number;
      selected: boolean;
    };

export interface TablePlan {
  columns: Columns;
  header: string;
  rows: TableRowPlan[];
  selectedRenderIndex: number;
  renderedRows: number;
  footer: string;
}
const ID_WIDTH = 15;
const ROUNDS_WIDTH = 8;
// Room for a trimmed value plus a short unit ("55434.2 ops/s"). A longer unit
// truncates here and reads in full from the hypothesis drill-down. Growing
// this further would push the fixed row past MEASURED_MIN_WIDTH.
const MEASURED_WIDTH = 20;
const OUTCOME_WIDTH = 11;
const KEPT_WIDTH = 4;
const COLUMN_GAP = '  ';
/** The one glyph the file uses for a value that is genuinely absent. */
const PLACEHOLDER = '—';

type Align = 'left' | 'right';

/**
 * One alignment per column, read by both `headerRow` and the cell renderers
 * (`entryCells`, `unownedRoundCells`) so a header label and its column's
 * values, placeholders included, share one source of truth and cannot drift
 * apart again. Numeric columns (Rounds, Measured, Kept) read right to left;
 * text columns (Hypothesis, Implementation Details, Outcome) read left to
 * right.
 */
const ID_ALIGN: Align = 'left';
const ROUNDS_ALIGN: Align = 'right';
const CLAIM_ALIGN: Align = 'left';
const MEASURED_ALIGN: Align = 'right';
const OUTCOME_ALIGN: Align = 'left';
const KEPT_ALIGN: Align = 'right';

export function resolveColumns(width: number): Columns {
  const claim = width >= CLAIM_MIN_WIDTH;
  const measured = width >= MEASURED_MIN_WIDTH;
  const kept = width >= KEPT_MIN_WIDTH;
  const fixed =
    ID_WIDTH +
    ROUNDS_WIDTH +
    OUTCOME_WIDTH +
    (measured ? MEASURED_WIDTH : 0) +
    (kept ? KEPT_WIDTH : 0);
  const visibleColumns = 3 + (claim ? 1 : 0) + (measured ? 1 : 0) + (kept ? 1 : 0);
  const gutterWidth = (visibleColumns - 1) * COLUMN_GAP.length;
  return {
    claim,
    measured,
    kept,
    // Exactly the remaining width, so the row fills the panel without
    // overflowing it and losing the trailing columns to truncation.
    claimWidth: claim ? Math.max(20, width - fixed - gutterWidth) : 0,
  };
}

export function headerRow(columns: Columns, direction: 'max' | 'min' | null = null): string {
  const parts = [
    fitColumn(' Hypothesis', ID_WIDTH, ID_ALIGN),
    fitColumn('Rounds', ROUNDS_WIDTH, ROUNDS_ALIGN),
  ];
  if (columns.claim) {
    parts.push(fitColumn('Implementation Details', columns.claimWidth, CLAIM_ALIGN));
  }
  if (columns.measured) {
    // The glyph is the way improvement points, so a signed delta below reads
    // as good or bad without task knowledge. A glyph rather than color, which
    // the outcome column already spends; the drill-down spells out the word.
    const label = direction === null ? 'Measured' : `Measured ${direction === 'max' ? '↑' : '↓'}`;
    parts.push(fitColumn(label, MEASURED_WIDTH, MEASURED_ALIGN));
  }
  parts.push(fitColumn('Outcome', OUTCOME_WIDTH, OUTCOME_ALIGN));
  if (columns.kept) parts.push(fitColumn('Kept', KEPT_WIDTH, KEPT_ALIGN));
  return parts.join(COLUMN_GAP);
}

/**
 * The selection caret shared by every selectable row in the log: `'›'` for the
 * selected row, a matching blank otherwise, so a row's other columns land in
 * the same place whether or not it is selected. Independent of any
 * status/active glyph the row also carries, following the precedent in
 * theme-picker.ts and the hypothesis drill-down (`#renderDetail`), where the
 * background swap alone was not enough to read selection on a low-contrast
 * terminal.
 */
export function selectionCaret(isSelected: boolean): string {
  return isSelected ? '›' : ' ';
}

/**
 * The two-character leading marker on a hypothesis row: the selection caret,
 * then the active-hypothesis marker. The two are independent signals, so both
 * render in the same row without either overwriting the other.
 */
export function entryLeadingMarker(entry: ExperimentEntry, isSelected: boolean): string {
  const active = entry.active === true ? '▸' : ' ';
  return `${selectionCaret(isSelected)}${active}`;
}

/**
 * The row split into the segments the view colors independently. Widths are
 * baked in so the segments still line up as separate renderables.
 */
export interface EntryCells {
  leading: string;
  outcome: string;
  trailing: string;
}

export function entryCells(
  entry: ExperimentEntry,
  columns: Columns,
  isSelected = false,
): EntryCells {
  const marker = entryLeadingMarker(entry, isSelected);
  const leading = [
    fitColumn(
      `${marker}${truncate(entry.hypothesis_id, ID_WIDTH - displayWidth(marker))}`,
      ID_WIDTH,
      ID_ALIGN,
    ),
    fitColumn(formatRounds(entry), ROUNDS_WIDTH, ROUNDS_ALIGN),
  ];
  if (columns.claim) {
    leading.push(
      fitColumn(
        sentenceCase(entry.title ?? entry.claim ?? entry.action ?? PLACEHOLDER),
        columns.claimWidth,
        CLAIM_ALIGN,
      ),
    );
  }
  if (columns.measured) {
    leading.push(fitColumn(formatMeasured(entry), MEASURED_WIDTH, MEASURED_ALIGN));
  }
  return {
    leading: leading.join(COLUMN_GAP),
    // These are separate renderables so outcome can carry semantic color.
    // Put gutters on the following segment rather than relying on trailing
    // padding surviving across renderable boundaries.
    outcome: `${COLUMN_GAP}${fitColumn(outcomeLabel(entry), OUTCOME_WIDTH, OUTCOME_ALIGN)}`,
    trailing: columns.kept
      ? `${COLUMN_GAP}${fitColumn(
          entry.kept === true ? 'Yes' : entry.kept === false ? 'No' : PLACEHOLDER,
          KEPT_WIDTH,
          KEPT_ALIGN,
        )}`
      : '',
  };
}

/**
 * The unowned-round row's cells, laid out on the exact same column grid
 * `entryCells` uses: same widths, same gap, same two-character marker slot.
 * A round with agent turns but no owning hypothesis is the common landing
 * state (a task profiles before proposing its first hypothesis), not an edge
 * case, so it has to read as a row under the same headers, not as one string
 * spanning all of them. The round number and "recorded agent turns" are real;
 * everything the round genuinely does not have yet (hypothesis, measurement,
 * outcome, kept) renders as `PLACEHOLDER`, not an invented value.
 */
export function unownedRoundCells(
  roundNumber: number,
  columns: Columns,
  isSelected = false,
): EntryCells {
  // No active-hypothesis glyph applies to a round with no hypothesis, but the
  // marker still reserves the same two columns `entryLeadingMarker` does, so
  // the identity text starts at the same offset as a hypothesis row's.
  const marker = `${selectionCaret(isSelected)} `;
  const leading = [
    fitColumn(
      `${marker}${truncate(NO_HYPOTHESIS_LABEL, ID_WIDTH - displayWidth(marker))}`,
      ID_WIDTH,
      ID_ALIGN,
    ),
    fitColumn(String(roundNumber), ROUNDS_WIDTH, ROUNDS_ALIGN),
  ];
  if (columns.claim) leading.push(fitColumn(RECORDED_TURNS_LABEL, columns.claimWidth, CLAIM_ALIGN));
  if (columns.measured) leading.push(fitColumn(PLACEHOLDER, MEASURED_WIDTH, MEASURED_ALIGN));
  return {
    leading: leading.join(COLUMN_GAP),
    outcome: `${COLUMN_GAP}${fitColumn(PLACEHOLDER, OUTCOME_WIDTH, OUTCOME_ALIGN)}`,
    trailing: columns.kept ? `${COLUMN_GAP}${fitColumn(PLACEHOLDER, KEPT_WIDTH, KEPT_ALIGN)}` : '',
  };
}

export function unownedRoundRow(roundNumber: number, columns: Columns, isSelected = false): string {
  const cells = unownedRoundCells(roundNumber, columns, isSelected);
  return `${cells.leading}${cells.outcome}${cells.trailing}`;
}

const NO_HYPOTHESIS_LABEL = '(no hypothesis)';
const RECORDED_TURNS_LABEL = 'recorded agent turns';

/**
 * Pads (or right-pads) `value` to `width` after truncating it, so every
 * caller shares one place that decides how a column fits. `align: 'right'`
 * is the smallest addition this needed: same truncation, same width budget,
 * padding placed before the value instead of after. Left is the default so
 * the many existing left-aligned calls are unchanged. Both directions measure
 * in cells via `displayWidth`, not code units, so CJK content still lines up.
 */
function fitColumn(value: string, width: number, align: Align = 'left'): string {
  const fitted = truncate(value, width);
  if (align === 'left') return padToWidth(fitted, width);
  return ' '.repeat(Math.max(0, width - displayWidth(fitted))) + fitted;
}

/**
 * Green for a hypothesis that held, red for one that did not, and the active
 * accent while it is still open. Outcomes with no such reading stay in body
 * text rather than being forced into a verdict: `inconclusive`, where a
 * trusted measurement did not decide the claim, and `unmeasured`, where the
 * framework measured nothing to decide it with.
 */
export function outcomeColor(theme: Theme, entry: ExperimentEntry): string {
  const outcome = entry.resolved_outcome ?? null;
  if (entry.active === true) return theme.warning;
  if (outcome === null) return theme.textPrimary;
  if (outcome === 'proven') return theme.success;
  if (outcome === 'disproven' || outcome === 'rejected') return theme.error;
  return theme.textPrimary;
}

/** Map backend resolution terms to concise operator-facing hypothesis decisions. */
export function outcomeLabel(entry: ExperimentEntry): string {
  if (entry.active === true) return 'Active';
  if (entry.resolved_outcome === 'proven') return 'Accepted';
  if (entry.resolved_outcome === 'disproven') return 'Rejected';
  return sentenceCase(entry.resolved_outcome ?? PLACEHOLDER);
}

/** Capitalises a wire value for display without touching the rest of it. */
export function sentenceCase(value: string): string {
  const index = value.search(/[a-z]/i);
  if (index === -1) return value;
  return value.slice(0, index) + value.charAt(index).toUpperCase() + value.slice(index + 1);
}

export function formatRounds(entry: ExperimentEntry): string {
  return entry.first_round === entry.last_round
    ? String(entry.first_round)
    : `${entry.first_round}-${entry.last_round}`;
}

/**
 * Delta wins when present. `not_framework_measured` is checked next, before
 * the metric fallback, because the entry-level `perf_metric` is null in that
 * case anyway. A `baseline_unresolved` absolute value gets a `? ` prefix, not
 * a suffix, so the MEASURED_WIDTH truncation in `fitColumn` can never cut it
 * off. `no_baseline_yet` and a legacy entry with no reason keep the bare
 * value: a first measurement is itself a deliberate absolute display.
 */
export function formatMeasured(entry: ExperimentEntry): string {
  const delta = entry.perf_delta_pct;
  if (typeof delta === 'number') return formatDelta(delta);
  if (entry.perf_delta_reason === 'not_framework_measured') return 'self-reported';
  if (typeof entry.perf_metric === 'number') {
    const marker = entry.perf_delta_reason === 'baseline_unresolved' ? '? ' : '';
    return `${marker}${trimNumber(entry.perf_metric)}${entry.perf_unit ? ` ${entry.perf_unit}` : ''}`;
  }
  return PLACEHOLDER;
}

function formatDelta(delta: number): string {
  const sign = delta > 0 ? '+' : '';
  return `${sign}${delta.toFixed(delta >= 10 || delta <= -10 ? 0 : 1)}%`;
}

/**
 * The one improvement direction the header can honestly carry. Null when no
 * entry recorded a direction, or when entries disagree, where a single glyph
 * would mislabel some rows.
 */
export function measuredDirection(entries: readonly ExperimentEntry[]): 'max' | 'min' | null {
  let direction: 'max' | 'min' | null = null;
  for (const entry of entries) {
    const candidate = entry.perf_direction ?? null;
    if (candidate === null) continue;
    if (direction === null) direction = candidate;
    else if (direction !== candidate) return null;
  }
  return direction;
}

export function hypothesisMetadata(entry: ExperimentEntry): string {
  const parts = [`Rounds ${formatRounds(entry)}`];
  if (entry.judge_verdict !== null && entry.judge_verdict !== undefined) {
    parts.push(`Judge ${sentenceCase(entry.judge_verdict)}`);
  }
  parts.push(`Decision ${outcomeLabel(entry)}`);
  if (entry.kept === true) parts.push('Candidate kept');
  else if (entry.kept === false) parts.push('Candidate reverted');
  parts.push(...measurementMetadata(entry));
  return parts.join(' · ');
}

/**
 * The measurement spelled out where width is unbounded: metric identity and
 * direction as words, then the absolute value, its baseline, and the causal
 * delta the table compresses into one cell.
 */

function measurementMetadata(entry: ExperimentEntry): string[] {
  const parts: string[] = [];
  const name = entry.perf_metric_name ?? null;
  const direction = measurementDirection(entry.perf_direction);
  if (name !== null) parts.push(`Metric ${name}${direction === null ? '' : ` (${direction})`}`);
  else if (direction !== null) parts.push(`Direction ${direction}`);
  // Legacy rounds recorded the metric name as the unit; once the name clause
  // carries that identity, repeating it after each number is noise.
  const unit = entry.perf_unit && entry.perf_unit !== name ? ` ${entry.perf_unit}` : '';
  if (typeof entry.perf_metric === 'number') {
    parts.push(`Measured ${trimNumber(entry.perf_metric)}${unit}`);
  }
  if (typeof entry.perf_baseline_value === 'number') {
    parts.push(`Baseline ${trimNumber(entry.perf_baseline_value)}${unit}`);
  }
  if (typeof entry.perf_baseline_round === 'number') {
    parts.push(`Baseline round ${entry.perf_baseline_round}`);
  }
  if (entry.perf_baseline_commit) {
    parts.push(`Baseline commit ${entry.perf_baseline_commit.slice(0, 7)}`);
  }
  if (typeof entry.perf_delta_pct === 'number') {
    parts.push(`Delta ${formatDelta(entry.perf_delta_pct)}`);
  }
  const reason = deltaReasonLabel(entry.perf_delta_reason);
  if (reason !== null) parts.push(reason);
  return parts;
}

export function planLogRenderMode(state: SessionState): LogRenderMode {
  const detail = detailedHypothesis(state);
  if (detail !== null) return {kind: 'detail', entry: detail};
  const log = state.experimentLog;
  if (log?.error !== null && log?.error !== undefined) return {kind: 'error', message: log.error};
  if (log?.pending === true) return {kind: 'loading'};
  const activity = hypothesisPlanningActivity(state);
  const unowned = unownedExperimentRounds(state);
  if (log?.entries.length === 0 && activity !== null && unowned.length === 0)
    return {kind: 'kickoff', activity};
  if (log?.entries.length === 0 && unowned.length === 0) return {kind: 'empty'};
  return {kind: 'table'};
}

export function planTable(state: SessionState, bodyWidth: number): TablePlan | null {
  const log = state.experimentLog;
  if (log === null) return null;
  const columns = resolveColumns(bodyWidth);
  const items = experimentIndexItems(state);
  const selected = selectedExperimentIndexItem(state);
  const rowPlan = planTableRows(log.entries, items, selected?.key);
  const selectedIndex = selected === null ? 0 : items.findIndex(item => item.key === selected.key);
  const position = `${Math.max(0, selectedIndex) + 1}/${items.length}`;
  const hint =
    bodyWidth >= HINT_MIN_WIDTH
      ? '↑↓ or scroll: select · Enter or click: open hypothesis'
      : '↑↓ · Enter';
  return {
    columns,
    header: headerRow(columns, measuredDirection(log.entries)),
    rows: rowPlan.rows,
    selectedRenderIndex: rowPlan.selectedRenderIndex,
    renderedRows: rowPlan.renderedRows,
    footer:
      log.entries.length === 0
        ? 'The first hypothesis appears once the orchestrator forms one.'
        : `${position} · ${hint}`,
  };
}

function planTableRows(
  entries: readonly ExperimentEntry[],
  items: readonly ExperimentIndexItem[],
  selectedKey: string | undefined,
): Pick<TablePlan, 'rows' | 'selectedRenderIndex' | 'renderedRows'> {
  const rows: TableRowPlan[] = [];
  let selectedRenderIndex = 0;
  let renderedRows = 0;
  for (const [index, item] of items.entries()) {
    const selected = item.key === selectedKey;
    const itemRows = tableRowsForItem(item, index, entries, selected);
    rows.push(...itemRows);
    if (selected) selectedRenderIndex = renderedRows + (item.kind === 'activity' ? 1 : 0);
    renderedRows += itemRows.length;
  }
  return {rows, selectedRenderIndex, renderedRows};
}

function tableRowsForItem(
  item: ExperimentIndexItem,
  index: number,
  entries: readonly ExperimentEntry[],
  selected: boolean,
): TableRowPlan[] {
  if (item.kind === 'hypothesis') {
    return [
      {
        kind: 'hypothesis',
        entry: item.entry,
        key: item.key,
        index: entries.indexOf(item.entry),
        selected,
      },
    ];
  }
  if (item.kind === 'round') {
    return [{kind: 'round', roundNumber: item.roundNumber, index, selected}];
  }
  return [
    {kind: 'activity-heading'},
    {kind: 'activity', item, existingHypotheses: entries.length, selected},
  ];
}

function measurementDirection(
  direction: ExperimentEntry['perf_direction'],
): 'maximize' | 'minimize' | null {
  if (direction === 'max') return 'maximize';
  if (direction === 'min') return 'minimize';
  return null;
}

/**
 * Spells out why `perf_delta_pct` is absent. Exhaustive over the wire union
 * with no default case, so a reason value the client does not yet know how
 * to word fails the build instead of silently rendering nothing.
 */
function deltaReasonLabel(reason: ExperimentEntry['perf_delta_reason']): string | null {
  switch (reason) {
    case 'no_baseline_yet':
      return 'No baseline existed yet';
    case 'baseline_unresolved':
      return 'No trusted baseline resolved';
    case 'not_framework_measured':
      return 'Self-reported, not framework-measured';
    case null:
    case undefined:
      return null;
  }
}

export function roundMetadata(roundNumber: number, round: ExperimentRound | undefined): string {
  const parts = [`Round ${roundNumber}`];
  if (round !== undefined) parts.push(`Judge ${judgeLabel(round)}`);
  if (typeof round?.perf_metric === 'number') {
    parts.push(`${trimNumber(round.perf_metric)}${round.perf_unit ? ` ${round.perf_unit}` : ''}`);
  }
  return parts.join(' · ');
}

/**
 * The round's own review state. `judge_verdict` is authoritative; a record
 * written before the framework stored one carries only `reviewed`, and
 * `passed` is the closest thing it has to a verdict.
 */
function judgeLabel(round: ExperimentRound): string {
  if (round.judge_verdict) return round.judge_verdict;
  return round.reviewed ? (round.passed ? 'pass' : 'fail') : 'pending';
}

export function planningHypothesisLabel(existingHypotheses: number): string {
  return `Hypothesis ${existingHypotheses + 1}`;
}

export function kickoffStages(
  stage: HypothesisPlanningActivity['stage'],
  hypothesis: string,
): Array<{marker: string; label: string; current: boolean}> {
  const current = (target: HypothesisPlanningActivity['stage']): boolean => stage === target;
  const completed = (target: HypothesisPlanningActivity['stage']): boolean =>
    (stage === 'profile' || stage === 'plan') && target === 'pre';
  return [
    {
      marker: current('pre') ? '●' : completed('pre') ? '✓' : '○',
      label: 'Decide whether profiling is needed',
      current: current('pre'),
    },
    {
      marker: current('profile') ? '●' : '○',
      label: current('profile') ? `Profile before ${hypothesis}` : 'Profile if needed',
      current: current('profile'),
    },
    {
      marker: current('plan') ? '●' : '○',
      label: `Form ${hypothesis}`,
      current: current('plan'),
    },
  ];
}

export function planningStageSummary(stage: HypothesisPlanningActivity['stage']): string {
  const labels: Record<HypothesisPlanningActivity['stage'], string> = {
    pre: 'deciding whether profiling is needed',
    profile: 'profiling',
    plan: 'forming it',
  };
  return labels[stage];
}

export function rowId(index: number): string {
  return `experiment-row-${index}`;
}

/**
 * At most `width` cells, ellipsized. Measured in cells, not code units: a CJK
 * value that fits its column by `String.length` can still be twice as wide on
 * screen, and slicing by code units can land inside a wide character.
 */
function truncate(value: string, width: number): string {
  if (width <= 1) return '';
  if (displayWidth(value) <= width) return value;
  return `${truncateToWidth(value, width - 1)}…`;
}

function trimNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, '');
}
