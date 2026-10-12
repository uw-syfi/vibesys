/** The policy-owned iteration navigation rail. */

import type {CoreState, IterationNavigationState} from '@vibesys/run-state';
import type {JSX} from 'react';

type IterationRoot = IterationNavigationState['byId'][string];

export interface RoundRailProps {
  readonly run: CoreState;
  readonly roots: readonly IterationRoot[];
  readonly selectedId: string | null;
  readonly onSelect: (iterationId: string | null) => void;
}

export function RoundRail({run, roots, selectedId, onSelect}: RoundRailProps): JSX.Element {
  return (
    <aside className="round-rail panel" aria-label="Iterations and status">
      <div className="panel-heading">
        <h2>Iterations</h2>
        <span>{roots.length} tracked</span>
      </div>
      {roots.length === 0 ? (
        <p className="empty">Waiting for the run to open its first iteration.</p>
      ) : (
        <ol className="round-list">
          <li>
            <button
              type="button"
              className="round-link"
              aria-pressed={selectedId === null}
              onClick={() => onSelect(null)}
            >
              <span>All activity</span>
              <span className="round-status">{run.transcript.length} events</span>
            </button>
          </li>
          {roots.map(iteration => (
            <li key={iteration.id}>
              <button
                type="button"
                className="round-link"
                aria-pressed={selectedId === iteration.id}
                onClick={() => onSelect(iteration.id)}
              >
                <span>{iteration.label}</span>
                <span className={`round-status round-status-${iteration.status}`}>
                  {iteration.status}
                </span>
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
