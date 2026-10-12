import type {CoreState} from '@vibesys/run-state';
import type {JSX} from 'react';

/** The run's counters: sequence, rounds, and transcript length. */
export function RunSummary({run}: {readonly run: CoreState}): JSX.Element {
  return (
    <section className="summary" aria-label="Run summary">
      <div>
        <span>Sequence</span>
        <strong>{run.sequence}</strong>
      </div>
      <div>
        <span>Rounds</span>
        <strong>
          {run.rounds.length}
          {run.maxRounds === null ? '' : ` / ${run.maxRounds}`}
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
