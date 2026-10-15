/** The browser and desktop run pages compose features through their public views and stores. */
import {
  CampaignDashboard,
  createCampaignReplayStore,
  createLiveCampaignStore,
} from '@vibesys/feature-campaign/renderer';
import {
  type CampaignRecord,
  type CampaignStream,
  webSessionCampaignStream,
} from '@vibesys/feature-campaign/state';
import {ConversationPanel} from '@vibesys/feature-conversation/renderer';
import {
  createGatewayConnectStore,
  type GatewayBootstrapTarget,
  type GatewayConnectAction,
  type GatewayConnection,
  type GatewayConnectState,
  GatewayForm,
} from '@vibesys/feature-gateway-connect/renderer';
import {
  createStartupLogStore,
  RunPage,
  type StartupLogStore,
} from '@vibesys/feature-run-view/renderer';
import {type StartupLogView, startupLogObservation} from '@vibesys/feature-run-view/state';
import type {RunLogSource} from '@vibesys/logs';
import type {RunStore, WebSession} from '@vibesys/run-session';
import {type CoreState, hasRunEnded} from '@vibesys/run-state';
import type {Store} from '@vibesys/store';
import type {ScheduleTimeout} from '@vibesys/transport';
import {Button, Dialog} from '@vibesys/ui/legacy';
import {type JSX, type ReactNode, useEffect, useState, useSyncExternalStore} from 'react';
import {bootstrapGateway, fetchCampaignFixture} from '../platform/backend.js';
import type {WebThemeController} from './theme.js';
import {WebThemePicker} from './theme-picker.js';
import {useNow} from './use-now.js';
import '@vibesys/ui/tokens.css';
// Each feature styles its own views; the page frame is the entry's `styles.css`.
import '@vibesys/feature-gateway-connect/styles.css';
import '@vibesys/feature-run-view/styles.css';
import '@vibesys/feature-campaign/styles.css';
import '@vibesys/feature-conversation/styles.css';

function useStore<S>(store: Pick<Store<S, unknown>, 'getState' | 'subscribe'>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

const NOTHING = (): void => undefined;
const SYSTEM_CLOCK = (): number => Date.now();
type CampaignStore = ReturnType<typeof createCampaignReplayStore>;
type GatewayStore = Store<GatewayConnectState, GatewayConnectAction>;

/** The page owner closes its stores before replacing the view or leaving the document. */
export interface PreviewPage {
  readonly view: JSX.Element;
  close(): void;
}

export interface DemoAppOptions {
  readonly page: string;
  readonly onGatewayConnected: (connection: GatewayConnection) => void;
  readonly initialGatewayError?: string | null;
  readonly initialGatewayTarget?: GatewayBootstrapTarget | null;
  /** The page-owned preference survives replacement of replay and live run surfaces. */
  readonly theme: WebThemeController;
}

export interface DemoAppServices {
  readonly bootstrap: (capabilityUrl: string, signal: AbortSignal) => Promise<string>;
  readonly loadCampaign: (signal: AbortSignal) => Promise<CampaignRecord>;
}

const DEFAULT_DEMO_SERVICES: DemoAppServices = {
  bootstrap: (capabilityUrl, signal) => bootstrapGateway(capabilityUrl, {signal}),
  loadCampaign: fetchCampaignFixture,
};

/** The campaign preview, retaining the current gateway bootstrap and retry behavior. */
export function createDemoApp(
  {
    page,
    onGatewayConnected,
    initialGatewayError = null,
    initialGatewayTarget = null,
    theme,
  }: DemoAppOptions,
  services: DemoAppServices = DEFAULT_DEMO_SERVICES,
): PreviewPage {
  const campaign = createCampaignReplayStore(signal => services.loadCampaign(signal));
  const gateway = createGatewayConnectStore({
    page,
    initialError: initialGatewayError,
    initialTarget: initialGatewayTarget,
    onConnected: connection => {
      campaign.close();
      onGatewayConnected(connection);
    },
    bootstrap: services.bootstrap,
  });
  campaign.dispatch({type: 'load'});
  return {
    view: (
      <WebAppShell theme={theme}>
        <Demo
          campaign={campaign}
          gateway={gateway}
          initialOpen={initialGatewayError !== null || initialGatewayTarget !== null}
        />
      </WebAppShell>
    ),
    close: () => {
      gateway.dispatch({type: 'cancel'});
      campaign.close();
    },
  };
}

function Demo({
  campaign,
  gateway,
  initialOpen,
}: {
  readonly campaign: CampaignStore;
  readonly gateway: GatewayStore;
  readonly initialOpen: boolean;
}): JSX.Element {
  const [open, setOpen] = useState(initialOpen);
  const state = useStore(gateway);
  const dismiss = (): void => {
    if (state.kind === 'connecting') gateway.dispatch({type: 'cancel'});
    setOpen(false);
  };
  return (
    <>
      <Campaign
        store={campaign}
        previewLabel="Preview"
        headerAction={
          <Button id="gateway-connect-trigger" onClick={() => setOpen(true)}>
            Connect
          </Button>
        }
      />
      <Dialog
        open={open}
        label="Connect to a run"
        dismiss={dismiss}
        focusFallbackId="gateway-connect-trigger"
      >
        <div className="gateway-dialog">
          <header>
            <h2>Connect to a run</h2>
            <Button onClick={dismiss} aria-label="Close gateway connection">
              ×
            </Button>
          </header>
          <p>Paste a gateway capability URL to open its run in this viewer.</p>
          <GatewayForm state={state} send={gateway.dispatch} />
        </div>
      </Dialog>
    </>
  );
}

/** Loading/error chrome around `CampaignDashboard`, by which of its three sources is live: a
 * fixture replay ('Preview'), the dev-only fixture-replayed-as-a-stream path ('Stream preview'),
 * or (`undefined`) a real desktop run's own live campaign stream. */
interface CampaignChrome {
  readonly testid: 'replay-banner' | 'campaign-stream-banner' | 'live-campaign-banner';
  readonly ariaLabel: string;
  readonly errorPrefix: string;
  readonly loadFailedCopy: string;
  readonly waitingCopy: string;
  readonly showRetry: boolean;
}

function campaignChrome(previewLabel: 'Preview' | 'Stream preview' | undefined): CampaignChrome {
  if (previewLabel === 'Preview') {
    return {
      testid: 'replay-banner',
      ariaLabel: 'Replay status',
      errorPrefix: 'Replay failed to load',
      loadFailedCopy: 'The replay fixture could not be loaded.',
      waitingCopy: 'Loading fixture replay…',
      showRetry: true,
    };
  }
  if (previewLabel === 'Stream preview') {
    return {
      testid: 'campaign-stream-banner',
      ariaLabel: 'Live stream status',
      errorPrefix: 'Live campaign stream error',
      loadFailedCopy: 'The replay fixture could not be loaded.',
      waitingCopy: 'Waiting for the live campaign stream…',
      showRetry: false,
    };
  }
  return {
    testid: 'live-campaign-banner',
    ariaLabel: 'Live campaign status',
    errorPrefix: 'Live campaign error',
    loadFailedCopy: 'The live campaign could not be loaded.',
    waitingCopy: 'Waiting for the live campaign…',
    showRetry: false,
  };
}

function Campaign({
  store,
  previewLabel,
  headerAction,
}: {
  readonly store: CampaignStore;
  readonly previewLabel?: 'Preview' | 'Stream preview';
  readonly headerAction: ReactNode;
}): JSX.Element {
  const {scenario, campaign, error} = useStore(store);
  const chrome = campaignChrome(previewLabel);
  return (
    <>
      {error !== null && (
        <div
          className="stale-banner campaign-alert"
          role="alert"
          aria-label={chrome.ariaLabel}
          data-testid={chrome.testid}
        >
          <span>
            {chrome.errorPrefix}: {error}
          </span>
          {chrome.showRetry && (
            <button type="button" onClick={() => store.dispatch({type: 'load'})}>
              Retry
            </button>
          )}
        </div>
      )}
      {scenario === null || campaign === null ? (
        <main className="campaign-loading">
          {headerAction}
          <p>{error !== null ? chrome.loadFailedCopy : chrome.waitingCopy}</p>
        </main>
      ) : (
        <CampaignDashboard
          scenario={scenario}
          campaign={campaign}
          {...(previewLabel === undefined ? {} : {previewLabel})}
          headerAction={headerAction}
        />
      )}
    </>
  );
}

/** Dev-only frame streaming uses the same campaign view as the fixture preview. */
export function createLiveCampaignApp(stream: CampaignStream): PreviewPage {
  const store = createLiveCampaignStore(stream);
  return {
    view: <Campaign store={store} previewLabel="Stream preview" headerAction={null} />,
    close: store.close,
  };
}

/**
 * The live page over `session`, which this starts, shared with the desktop entry. `logs` is the
 * run's log source where the page can read logs (the desktop shell); without one the page shows no
 * startup log panel.
 */
export function createLiveApp(
  session: WebSession,
  theme: WebThemeController,
  logs: RunLogSource | null = null,
): JSX.Element {
  void session.start();
  return (
    <WebAppShell theme={theme}>
      <Live session={session} run={session.store} logs={logs} />
    </WebAppShell>
  );
}

/** The browser's timers, for the startup log panel's retries. */
const BROWSER_SCHEDULE: ScheduleTimeout = (callback, delayMs) => {
  const timer = setTimeout(callback, delayMs);
  return () => clearTimeout(timer);
};
const NO_SUBSCRIPTION = (): (() => void) => NOTHING;
const NO_VIEW = (): StartupLogView | null => null;

/**
 * The startup log panel's store for this page, created and closed with the page's effect so a
 * remount (StrictMode included) never reuses a closed store, and told what the run says about
 * the panel each time that changes.
 */
function useStartupLog(
  logs: RunLogSource | null,
  run: CoreState,
): {readonly view: StartupLogView; readonly send: StartupLogStore['send']} | null {
  const [store, setStore] = useState<StartupLogStore | null>(null);
  useEffect(() => {
    if (logs === null) return;
    const created = createStartupLogStore(logs, BROWSER_SCHEDULE);
    setStore(created);
    return () => {
      created.close();
      setStore(null);
    };
  }, [logs]);
  const {visible, stage} = startupLogObservation(run);
  useEffect(() => store?.send({type: 'observe', visible, stage}), [store, visible, stage]);
  const view = useSyncExternalStore(
    store?.subscribe ?? NO_SUBSCRIPTION,
    store?.getState ?? NO_VIEW,
    store?.getState ?? NO_VIEW,
  );
  return store === null || view === null ? null : {view, send: store.send};
}

function WebAppShell({
  theme,
  children,
}: {
  readonly theme: WebThemeController;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <>
      <WebThemePicker theme={theme} />
      {children}
    </>
  );
}

type RunView = 'run' | 'campaign';

function Live({
  session,
  run,
  logs,
}: {
  readonly session: WebSession;
  readonly run: RunStore;
  readonly logs: RunLogSource | null;
}): JSX.Element {
  const iterations = useSyncExternalStore(run.subscribe, run.getIterations, run.getIterations);
  const state = useStore(run);
  const sessionState = useStore(session);
  const [view, setView] = useState<RunView>('run');
  const now = useNow(SYSTEM_CLOCK, state.status === 'starting');
  const startupLog = useStartupLog(logs, state);
  // Created once for this page's lifetime, over the same session the run page reads: it
  // subscribes to `session.store`, which (unlike the session's own transport) supports multiple
  // independent subscribers, so this coexists with the run page's own subscription below.
  const [campaignStore] = useState(() =>
    createLiveCampaignStore(webSessionCampaignStream(session)),
  );
  useEffect(() => campaignStore.close, [campaignStore]);

  const toggle = (
    <Button
      id="run-campaign-toggle"
      onClick={() => setView(current => (current === 'run' ? 'campaign' : 'run'))}
    >
      {view === 'run' ? 'View campaign' : 'View run'}
    </Button>
  );

  if (view === 'campaign') {
    return <Campaign store={campaignStore} headerAction={toggle} />;
  }

  return (
    <RunPage
      run={state}
      iterations={iterations}
      session={sessionState}
      replayFailure={null}
      onReattach={() => session.reattach()}
      onReconnectControls={() => session.reconnectControls()}
      onRetryReplay={NOTHING}
      onCommand={session.command}
      now={now}
      startupLog={startupLog}
      headerAction={
        <>
          {toggle}
          <ConversationPanel
            session={session}
            runId={state.runId}
            transcript={state.chatTranscript}
            connected={sessionState.controls.status === 'connected'}
            steerAvailable={!hasRunEnded(state)}
          />
        </>
      }
    />
  );
}
