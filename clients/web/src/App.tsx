import {type FormEvent, type JSX, useEffect, useState, useSyncExternalStore} from 'react';
import {loadReplayFixture} from './replay.js';
import type {WebSession} from './session.js';
import {type CoreStateStore, createCoreStateStore} from './store.js';

const EMPTY_SESSION_STATE = {status: 'connected' as const, error: null};
const EMPTY_SESSION_SUBSCRIBE = (): (() => void) => () => undefined;

export function App({
  store,
  session,
}: {
  readonly store: CoreStateStore;
  readonly session?: WebSession;
}): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const sessionState = useSyncExternalStore(
    session?.subscribe ?? EMPTY_SESSION_SUBSCRIBE,
    session?.getState ?? (() => EMPTY_SESSION_STATE),
    session?.getState ?? (() => EMPTY_SESSION_STATE),
  );
  useEffect(() => {
    if (session === undefined) void loadReplayFixture(store).catch(() => undefined);
    else void session.start();
  }, [session, store]);
  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">VIBESYS / RUN VIEWER</p>
          <h1>{state.roundLabel ?? 'Replay is loading'}</h1>
        </div>
        <span className={`status status-${state.status}`}>{state.status}</span>
      </header>
      {sessionState.status === 'stale' && (
        <div className="stale-banner" role="alert">
          <span>
            Live connection is stale
            {sessionState.error === null ? '' : `: ${sessionState.error.message}`}
          </span>
          <button type="button" onClick={() => session?.reattach()}>
            Reattach
          </button>
        </div>
      )}
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
  return (
    <>
      <GatewayConnect />
      <App store={createCoreStateStore()} />
    </>
  );
}

export function createLiveApp(session: WebSession): JSX.Element {
  return <App store={session.store} session={session} />;
}

function GatewayConnect(): JSX.Element {
  const [gatewayUrl, setGatewayUrl] = useState('');
  const [error, setError] = useState<string | null>(null);

  const connect = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    try {
      const gateway = new URL(gatewayUrl.trim(), window.location.origin);
      if (!['http:', 'https:'].includes(gateway.protocol)) {
        throw new Error('Use an http:// or https:// gateway URL');
      }
      if (window.location.protocol === 'https:' && gateway.protocol !== 'https:') {
        throw new Error('An HTTPS browser page requires an HTTPS gateway URL');
      }
      if (!gateway.searchParams.has('token')) {
        throw new Error('The gateway URL must include its capability token');
      }
      const page = new URL(window.location.href);
      page.search = new URLSearchParams({gateway: gateway.toString()}).toString();
      window.location.assign(page.toString());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <form className="gateway-connect" onSubmit={connect}>
      <label htmlFor="gateway-url">Live gateway URL</label>
      <div className="gateway-connect-row">
        <input
          id="gateway-url"
          type="url"
          value={gatewayUrl}
          onChange={event => setGatewayUrl(event.target.value)}
          placeholder="http://127.0.0.1:8765/?token=..."
          spellCheck={false}
        />
        <button type="submit">Connect</button>
      </div>
      {error !== null && <p className="gateway-connect-error">{error}</p>}
    </form>
  );
}
