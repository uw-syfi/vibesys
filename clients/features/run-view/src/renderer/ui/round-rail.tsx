/** The semantic run-map navigation rail. It reads the shared round projection instead of parsing
 * labels, so live and replayed runs retain one meaning. */

import type {CoreState} from '@vibesys/run-state';
import {type RoundKey, roundsWithPlan, sameRoundKey} from '@vibesys/run-state/rounds';
import type {JSX} from 'react';

export interface RoundRailProps {
  readonly run: CoreState;
  readonly selected: RoundKey | null;
  readonly onSelect: (round: RoundKey | null) => void;
}

export function RoundRail({run, selected, onSelect}: RoundRailProps): JSX.Element {
  const rounds = roundsWithPlan(run.rounds, run.maxRounds);
  return (
    <aside className="round-rail panel" aria-label="Rounds and status">
      <div className="panel-heading">
        <h2>Rounds</h2>
        <span>{rounds.length} tracked</span>
      </div>
      {rounds.length === 0 ? (
        <p className="empty">Waiting for the run to open its first round.</p>
      ) : (
        <ol className="round-list">
          <li>
            <button
              type="button"
              className="round-link"
              aria-pressed={selected === null}
              onClick={() => onSelect(null)}
            >
              <span>All activity</span>
              <span className="round-status">{run.transcript.length} events</span>
            </button>
          </li>
          {rounds.map(round => (
            <li
              key={
                round.key.kind === 'number'
                  ? `number:${round.key.number}`
                  : `label:${round.key.label}`
              }
            >
              <button
                type="button"
                className="round-link"
                aria-pressed={sameRoundKey(selected, round.key)}
                onClick={() => onSelect(round.key)}
              >
                <span>
                  {round.key.kind === 'label' ? round.key.label : `Round ${round.number}`}
                </span>
                <span className={`round-status round-status-${round.status}`}>{round.status}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
      <dl className="run-status-list">
        <div>
          <dt>State</dt>
          <dd>{run.status}</dd>
        </div>
        <div>
          <dt>Active agents</dt>
          <dd>{Object.keys(run.activeExecutions).length}</dd>
        </div>
        <div>
          <dt>Diagnostics</dt>
          <dd>{run.diagnostics.length}</dd>
        </div>
      </dl>
    </aside>
  );
}
