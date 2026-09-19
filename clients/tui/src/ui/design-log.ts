import {
  CandidateDisposition,
  DesignChange,
  type DesignFileChange,
  HypothesisOutcome,
  RoundReviewVerdict,
} from '@vibesys/backend-client';
import type {DesignRoundView} from '../session-model.js';
import {enumWord} from './enum-word.js';

/**
 * Pure formatting for the per-round design log.
 *
 * Two surfaces share these helpers: the `/design` pane, which summarizes every
 * round in two lines, and the hypothesis drill-down, which shows the selected
 * round's full change list. Neither recomputes anything. The backend derives
 * file lists in the design query and every stage fact in the experiment query;
 * `designRoundViews` joins them by round, and this module only lays them out.
 */

/** File names shown inline in the summary before the rest become a count. */
const SUMMARY_FILE_LIMIT = 4;
/** Enough of a checkpoint hash to paste into git without dominating the row. */
const CHECKPOINT_WIDTH = 10;

export function fileChangeGlyph(change: DesignFileChange['change']): string {
  if (change === DesignChange.ADDED) return '+';
  if (change === DesignChange.DELETED) return '-';
  if (change === DesignChange.RENAMED) return '→';
  return '~';
}

export function formatFileChange(change: DesignFileChange): string {
  const glyph = fileChangeGlyph(change.change);
  if (change.change === DesignChange.RENAMED && change.renamedFrom) {
    return `${glyph} ${change.path} (was ${change.renamedFrom})`;
  }
  return `${glyph} ${change.path}`;
}

/** Compact per-kind tally, e.g. `+2 ~1 →1`, or null when nothing changed. */
export function fileChangeCounts(files: readonly DesignFileChange[]): string | null {
  const order: ReadonlyArray<DesignFileChange['change']> = [
    DesignChange.ADDED,
    DesignChange.MODIFIED,
    DesignChange.DELETED,
    DesignChange.RENAMED,
  ];
  const parts = order.flatMap(kind => {
    const count = files.filter(file => file.change === kind).length;
    return count > 0 ? [`${fileChangeGlyph(kind)}${count}`] : [];
  });
  return parts.length > 0 ? parts.join(' ') : null;
}

/**
 * The round's stage conclusions on one line: empirical outcome, review,
 * official evaluation, candidate decision, and the checkpoint that holds the
 * changes. Every value comes from the experiment log's own row for the round,
 * so this line and the table above it can never disagree. Null when the
 * experiment log has no row for the round.
 */
export function designStageSummary(view: DesignRoundView): string | null {
  const record = view.record;
  if (record === null) return null;
  const parts: string[] = [];
  const outcome = enumWord(HypothesisOutcome, record.hypothesisOutcome);
  if (outcome) parts.push(`Outcome ${outcome}`);
  const verdict = enumWord(RoundReviewVerdict, record.judgeVerdict);
  if (verdict) parts.push(`Judge ${verdict}`);
  if (record.officialEvaluation === true) parts.push('Official evaluation');
  const disposition = enumWord(CandidateDisposition, record.candidateDisposition);
  if (disposition) parts.push(`Candidate ${disposition}`);
  if (record.commit) parts.push(`Checkpoint ${record.commit.slice(0, CHECKPOINT_WIDTH)}`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/** `Round 3 · H-01 · Batch decode requests`, dropping what is not recorded. */
export function designRoundHeading(view: DesignRoundView): string {
  const parts = [`Round ${view.round}`];
  if (view.hypothesisId) parts.push(view.hypothesisId);
  const title = view.title?.trim();
  if (title) parts.push(title);
  return parts.join(' · ');
}

function measuredLabel(view: DesignRoundView): string | null {
  const record = view.record;
  if (record === null || typeof record.perfMetric !== 'number') return null;
  const value = Number.isInteger(record.perfMetric)
    ? String(record.perfMetric)
    : record.perfMetric.toFixed(2).replace(/\.?0+$/, '');
  const unit = record.perfUnit ? ` ${record.perfUnit}` : '';
  const delta = record.perfDeltaPct;
  const deltaLabel =
    typeof delta === 'number'
      ? ` (${delta > 0 ? '+' : ''}${delta.toFixed(Math.abs(delta) >= 10 ? 0 : 1)}%)`
      : '';
  return `${value}${unit}${deltaLabel}`;
}

function summaryFileLine(view: DesignRoundView): string {
  const files = view.files;
  if (files === null) return '  file changes not recorded';
  if (files.length === 0) return '  no workspace files changed';
  const counts = fileChangeCounts(files) ?? '';
  const names = files.slice(0, SUMMARY_FILE_LIMIT).map(file => file.path);
  const more = files.length - names.length;
  const suffix = more > 0 ? `, +${more} more` : '';
  return `  ${counts}  ${names.join(', ')}${suffix}`;
}

/**
 * The `/design` pane: every round in two lines, newest last. The pane scrolls,
 * so nothing is dropped to fit a box; the drill-down still carries the full
 * per-round file list.
 */
export function renderDesignSummary(views: readonly DesignRoundView[]): string {
  if (views.length === 0) {
    return [
      'Design changes by round',
      '',
      'No rounds have been recorded yet.',
      'Rounds appear here once the agent finishes its first one.',
    ].join('\n');
  }
  const lines = ['Design changes by round', ''];
  for (const view of views) {
    const measured = measuredLabel(view);
    const heading = measured
      ? `${designRoundHeading(view)} · ${measured}`
      : designRoundHeading(view);
    lines.push(heading, summaryFileLine(view));
  }
  lines.push(
    '',
    'Open a hypothesis and select a round for its full change list.',
    "Press d for the newest round's diff.",
  );
  return lines.join('\n');
}
