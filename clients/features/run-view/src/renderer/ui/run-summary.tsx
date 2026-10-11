import type {CoreState} from '@vibesys/core-state';
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
    </section>
  );
}

/** The newest transcript entries, or what an empty transcript means right now. */
export function Activity({
  run,
  emptyCopy,
}: {
  readonly run: CoreState;
  readonly emptyCopy: string;
}): JSX.Element {
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Activity</h2>
        <span>{run.transcript.length} folded events</span>
      </div>
      {run.transcript.length === 0 ? (
        <p className="empty">{emptyCopy}</p>
      ) : (
        <ol className="transcript">
          {run.transcript.slice(-8).map(entry => (
            <li key={entry.id}>
              <span className="marker" />
              <span className="transcript-entry">
                <span className="transcript-content">{entry.content}</span>
                {entry.truncated === true && (
                  <span className="event-truncated">
                    Event payload truncated at the server's size bound.
                  </span>
                )}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
