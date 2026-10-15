import {type CoreState, type IterationNavigationState, iterationRoots} from '@vibesys/run-state';
import type {JSX} from 'react';

/** The run's counters: sequence, rounds, and transcript length. */
export function RunSummary({
  run,
  iterations,
}: {
  readonly run: CoreState;
  readonly iterations: IterationNavigationState;
}): JSX.Element {
  const roots = iterationRoots(iterations, run);
  const roundBudget =
    roots.length > 0 && roots.every(root => root.kind === 'round') ? run.maxRounds : null;
  return (
    <section className="summary" aria-label="Run summary">
      <div>
        <span>Sequence</span>
        <strong>{run.sequence}</strong>
      </div>
      <div>
        <span>Iterations</span>
        <strong>
          {roots.length}
          {roundBudget === null ? '' : ` / ${roundBudget}`}
        </strong>
      </div>
      <div>
        <span>Transcript</span>
        <strong>{run.transcript.length} entries</strong>
      </div>
      <div>
        <span>Usage</span>
        <strong>{usageText(run)}</strong>
      </div>
      <div>
        <span>Result</span>
        <strong>{resultText(run)}</strong>
      </div>
    </section>
  );
}

function usageText(run: CoreState): string {
  const usage = run.usage;
  if (usage === null) return 'Not reported';
  const context =
    usage.contextWindow === null ? null : ` / ${usage.contextWindow.toLocaleString()}`;
  return `${usage.inputTokens.toLocaleString()}${context ?? ''}`;
}

/** Selected once per run, from `winner_selected`; `Pending` before that event. */
function resultText(run: CoreState): string {
  const winner = run.winner;
  if (winner === null) return 'Pending';
  if (winner.selection === 'baseline') return `Baseline kept (${shortRevision(winner.revision)})`;
  const settlement = winner.settlementId === null ? '' : ` · ${winner.settlementId}`;
  return `Candidate ${shortRevision(winner.revision)}${settlement}`;
}

function shortRevision(revision: string): string {
  return revision.slice(0, 7);
}
