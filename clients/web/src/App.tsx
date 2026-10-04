import {activeRunFocus, phaseText} from '@vibesys/core-state';
import {type FormEvent, type JSX, useEffect, useState, useSyncExternalStore} from 'react';
import {CampaignDashboard} from './CampaignDashboard.js';
import {connectionBanners} from './banners.js';
import {useCampaignHistory} from './campaign-history.js';
import type {CampaignRecord} from './campaign-record.js';
import {bootstrapGateway, GatewaySessionStore, targetFromCapability} from './gateway-session.js';
import {DEFAULT_REPLAY_FIXTURE_URL, loadReplayFixture} from './replay.js';
import {loadReplayScenario} from './replay-scenario.js';
import type {WebSession} from './session.js';
import {type CoreStateStore, createCoreStateStore} from './store.js';

const REPLAY_SCENARIO_URL = new URL('../dev/fixtures/trajectory-replay.json', import.meta.url).href;

export function App({
  store,
  session,
}: {
  readonly store: CoreStateStore;
  readonly session?: WebSession;
}): JSX.Element {
  return session === undefined ? <DemoReplay store={store} /> : <LiveReplay session={session} />;
}

function DemoReplay({store}: {readonly store: CoreStateStore}): JSX.Element {
  const [replayError, setReplayError] = useState<Error | null>(null);
  const [replayAttempt, setReplayAttempt] = useState(0);
  const [scenario, setScenario] = useState<Awaited<ReturnType<typeof loadReplayScenario>> | null>(
    null,
  );
  useEffect(() => {
    const abort = new AbortController();
    let active = true;
    setReplayError(null);
    setScenario(null);
    const replayUrl = `${DEFAULT_REPLAY_FIXTURE_URL}?attempt=${replayAttempt}`;
    void loadReplayFixture(store, replayUrl, abort.signal).catch(reason => {
      if (!abort.signal.aborted && active) setReplayError(toError(reason));
    });
    const scenarioUrl = new URL(REPLAY_SCENARIO_URL);
    scenarioUrl.searchParams.set('attempt', String(replayAttempt));
    void loadReplayScenario(scenarioUrl.toString())
      .then(value => {
        if (!abort.signal.aborted && active) setScenario(value);
      })
      .catch(reason => {
        if (!abort.signal.aborted && active) setReplayError(toError(reason));
      });
    return () => {
      active = false;
      abort.abort();
    };
  }, [replayAttempt, store]);

  return (
    <>
      {replayError !== null && (
        <ReplayErrorBanner
          error={replayError}
          onRetry={() => setReplayAttempt(attempt => attempt + 1)}
        />
      )}
      {scenario === null ? (
        <main className="campaign-loading">
          <span className="loading-mark" aria-hidden="true">
            V
          </span>
          <p>
            {replayError === null
              ? 'Loading fixture replay…'
              : 'The replay fixture could not be loaded.'}
          </p>
        </main>
      ) : (
        <LoadedCampaign scenario={scenario} />
      )}
    </>
  );
}

function LoadedCampaign({scenario}: {readonly scenario: CampaignRecord}): JSX.Element {
  const campaign = useCampaignHistory(scenario);
  return <CampaignDashboard scenario={scenario} campaign={campaign} />;
}

function ReplayErrorBanner({
  error,
  onRetry,
}: {
  readonly error: Error;
  readonly onRetry: () => void;
}): JSX.Element {
  return (
    <div
      className="stale-banner campaign-alert"
      role="alert"
      aria-label="Replay status"
      data-testid="replay-banner"
    >
      <span>Replay failed to load: {error.message}</span>
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

function LiveReplay({session}: {readonly session: WebSession}): JSX.Element {
  const store = session.store;
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const sessionState = useSyncExternalStore(session.subscribe, session.getState, session.getState);
  const banners = connectionBanners(state, sessionState);
  const active = activeRunFocus(state);
  const focus =
    active.length > 1
      ? `${active.length} agents active`
      : (phaseText(active[0]?.description ?? null) ??
        (state.status === 'connecting' ? 'Replay is loading' : 'Run overview'));
  useEffect(() => {
    void session.start();
  }, [session]);
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
            <button type="button" onClick={() => session.reattach()}>
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
          <button
            type="button"
            disabled={banners.controls.retrying}
            onClick={() => session.reconnectControls()}
          >
            {banners.controls.retrying ? 'Reconnecting…' : 'Reconnect now'}
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

function toError(reason: unknown): Error {
  return reason instanceof Error ? reason : new Error(String(reason));
}

export function createDemoApp(initialGatewayError: string | null = null): JSX.Element {
  return (
    <>
      <details className="gateway-connect-disclosure">
        <summary>Connect live gateway</summary>
        <GatewayConnect initialError={initialGatewayError} />
      </details>
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
