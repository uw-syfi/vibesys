import {type JSX, useEffect, useSyncExternalStore} from 'react';
import {loadReplayFixture} from './replay.js';
import {type CoreStateStore, createCoreStateStore} from './store.js';

export function App({store}: {readonly store: CoreStateStore}): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  useEffect(() => {
    void loadReplayFixture(store).catch(() => undefined);
  }, [store]);
  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">VIBESYS / RUN VIEWER</p>
          <h1>{state.roundLabel ?? 'Replay is loading'}</h1>
        </div>
        <span className={`status status-${state.status}`}>{state.status}</span>
      </header>
      <section className="summary" aria-label="Run summary">
        <div>
          <span>Sequence</span>
          <strong>{state.sequence}</strong>
        </div>
        <div>
          <span>Rounds</span>
          <strong>
            {state.rounds.length}
            {state.maxRounds === null ? '' : ` / ${state.maxRounds}`}
          </strong>
        </div>
        <div>
          <span>Transcript</span>
          <strong>{state.transcript.length} entries</strong>
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Activity</h2>
          <span>{state.transcript.length} folded events</span>
        </div>
        {state.transcript.length === 0 ? (
          <p className="empty">Waiting for the replay stream…</p>
        ) : (
          <ol className="transcript">
            {state.transcript.slice(-8).map(entry => (
              <li key={entry.id}>
                <span className="marker" />
                {entry.content}
              </li>
            ))}
          </ol>
        )}
      </section>
    </main>
  );
}

export function createDemoApp(): JSX.Element {
  return <App store={createCoreStateStore()} />;
}
