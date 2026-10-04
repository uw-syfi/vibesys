import {activeRunFocus, phaseText} from '@vibesys/core-state';
import {type FormEvent, type JSX, useEffect, useState, useSyncExternalStore} from 'react';
import {connectionBanners, emptyTranscriptCopy} from './banners.js';
import {bootstrapGateway, GatewaySessionStore, targetFromCapability} from './gateway-session.js';
import {DEFAULT_REPLAY_FIXTURE_URL, loadReplayFixture} from './replay.js';
import type {WebSession} from './session.js';
import {type CoreStateStore, createCoreStateStore} from './store.js';

const EMPTY_SESSION_STATE = {
  status: 'connected' as const,
  error: null,
  controls: {status: 'connected' as const},
};
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
  const banners = connectionBanners(state, sessionState);
  const emptyTranscript = emptyTranscriptCopy(state, banners);
  const active = activeRunFocus(state);
  const focus =
    active.length > 1
      ? `${active.length} agents active`
      : (phaseText(active[0]?.description ?? null) ??
        (state.status === 'connecting' ? 'Replay is loading' : 'Run overview'));
  const [replayError, setReplayError] = useState<Error | null>(null);
  const [replayAttempt, setReplayAttempt] = useState(0);
  useEffect(() => {
    if (session !== undefined) {
      void session.start();
      return;
    }
    const abort = new AbortController();
    setReplayError(null);
    const replayUrl = `${DEFAULT_REPLAY_FIXTURE_URL}?attempt=${replayAttempt}`;
    void loadReplayFixture(store, replayUrl, abort.signal).catch(reason => {
      if (!abort.signal.aborted) setReplayError(toError(reason));
    });
    return () => abort.abort();
  }, [replayAttempt, session, store]);
  return (
    <main className="shell">
      <header className="header">
        <div>
          <p className="eyebrow">VIBESYS / RUN VIEWER</p>
          <h1>{focus}</h1>
        </div>
        <span className={`status status-${state.status}`}>{state.status}</span>
      </header>
      {/*
        Each banner carries an `aria-label` as well as a `data-testid`. They are
        different mechanisms for different audiences: `data-testid` reaches
        tests and has no ARIA mapping at all, while the label is what names the
        region in the accessibility tree. Without one, three regions that share
        `role="alert"` and a class are indistinguishable to a screen reader, and
        `role="alert"` implies `aria-live="assertive"`, so two that are live at
        once interrupt each other unnamed.
      */}
      {banners.stream !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Event stream status"
          data-testid="stream-banner"
        >
          <span>{banners.stream.message}</span>
          {banners.stream.reattach && (
            <button type="button" onClick={() => session?.reattach()}>
              Reattach
            </button>
          )}
        </div>
      )}
      {banners.controls !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Run controls status"
          data-testid="controls-banner"
        >
          <span>{banners.controls.message}</span>
          {/*
            Disabled rather than hidden while a dial is in flight: `reconnect()`
            no-ops then, so an enabled button would swallow clicks for the whole
            connect-timeout window and change nothing on screen. Hiding it
            instead would make the affordance flicker in and out.
          */}
          <button
            type="button"
            disabled={banners.controls.retrying}
            onClick={() => session?.reconnectControls()}
          >
            {banners.controls.retrying ? 'Reconnecting…' : 'Reconnect now'}
          </button>
        </div>
      )}
      {replayError !== null && (
        <div
          className="stale-banner"
          role="alert"
          aria-label="Replay status"
          data-testid="replay-banner"
        >
          <span>Replay failed to load: {replayError.message}</span>
          <button type="button" onClick={() => setReplayAttempt(attempt => attempt + 1)}>
            Retry
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
          <p className="empty">{emptyTranscript}</p>
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

function toError(reason: unknown): Error {
  return reason instanceof Error ? reason : new Error(String(reason));
}

export function createDemoApp(initialGatewayError: string | null = null): JSX.Element {
  return (
    <>
      <GatewayConnect initialError={initialGatewayError} />
      <App store={createCoreStateStore()} />
    </>
  );
}

export function createLiveApp(session: WebSession): JSX.Element {
  return <App store={session.store} session={session} />;
}

function GatewayConnect({initialError}: {readonly initialError: string | null}): JSX.Element {
  const [gatewayUrl, setGatewayUrl] = useState('');
  const [error, setError] = useState<string | null>(initialError);
  const [connecting, setConnecting] = useState(false);

  const connect = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setConnecting(true);
    setError(null);
    void connectToGateway(gatewayUrl)
      .catch(reason => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setConnecting(false));
  };

  const connectToGateway = async (capabilityUrl: string): Promise<void> => {
    const target = targetFromCapability(window.location.href, capabilityUrl);
    if (target.bootstrapUrl === null) throw new Error('The gateway URL has no capability token');
    const browserSession = await bootstrapGateway(target.bootstrapUrl);
    const storedSessions = new GatewaySessionStore(() => window.sessionStorage);
    storedSessions.rememberGateway(target.gatewayUrl);
    storedSessions.rememberBrowserSession(target.gatewayUrl, browserSession);
    window.location.assign(target.cleanPageUrl);
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
        <button type="submit" disabled={connecting}>
          {connecting ? 'Connecting…' : 'Connect'}
        </button>
      </div>
      {error !== null && <p className="gateway-connect-error">{error}</p>}
    </form>
  );
}
