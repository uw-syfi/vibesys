/** The browser and desktop run pages compose features through their public views and stores. */
import {
  CampaignDashboard,
  createCampaignReplayStore,
  createLiveCampaignStore,
} from '@vibesys/feature-campaign/renderer';
import type {CampaignRecord, CampaignStream} from '@vibesys/feature-campaign/state';
import {ConversationPanel} from '@vibesys/feature-conversation/renderer';
import {
  createGatewayConnectStore,
  type GatewayBootstrapTarget,
  type GatewayConnectAction,
  type GatewayConnection,
  type GatewayConnectState,
  GatewayForm,
} from '@vibesys/feature-gateway-connect/renderer';
import {RunPage} from '@vibesys/feature-run-view/renderer';
import type {RunStore, WebSession} from '@vibesys/run-session';
import {hasRunEnded} from '@vibesys/run-state';
import type {Store} from '@vibesys/store';
import {Button, Dialog} from '@vibesys/ui';
import {type JSX, type ReactNode, useState, useSyncExternalStore} from 'react';
import {bootstrapGateway, fetchCampaignFixture} from '../platform/backend.js';
import '@vibesys/ui/tokens.css';
import '@vibesys/feature-gateway-connect/styles.css';
import '@vibesys/feature-run-view/styles.css';
import '@vibesys/feature-campaign/styles.css';
import '@vibesys/feature-conversation/styles.css';

function useStore<S>(store: Pick<Store<S, unknown>, 'getState' | 'subscribe'>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

const NOTHING = (): void => undefined;
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
      <Demo
        campaign={campaign}
        gateway={gateway}
        initialOpen={initialGatewayError !== null || initialGatewayTarget !== null}
      />
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

function Campaign({
  store,
  previewLabel,
  headerAction,
}: {
  readonly store: CampaignStore;
  readonly previewLabel: 'Preview' | 'Stream preview';
  readonly headerAction: ReactNode;
}): JSX.Element {
  const {scenario, campaign, error} = useStore(store);
  const streaming = previewLabel === 'Stream preview';
  return (
    <>
      {error !== null && (
        <div
          className="stale-banner campaign-alert"
          role="alert"
          aria-label={streaming ? 'Live stream status' : 'Replay status'}
          data-testid={streaming ? 'campaign-stream-banner' : 'replay-banner'}
        >
          <span>
            {streaming ? 'Live campaign stream error' : 'Replay failed to load'}: {error}
          </span>
          {!streaming && (
            <button type="button" onClick={() => store.dispatch({type: 'load'})}>
              Retry
            </button>
          )}
        </div>
      )}
      {scenario === null || campaign === null ? (
        <main className="campaign-loading">
          {headerAction}
          <p>
            {error !== null
              ? 'The replay fixture could not be loaded.'
              : streaming
                ? 'Waiting for the live campaign stream…'
                : 'Loading fixture replay…'}
          </p>
        </main>
      ) : (
        <CampaignDashboard
          scenario={scenario}
          campaign={campaign}
          previewLabel={previewLabel}
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

/** The live page over the existing session machine, shared with the desktop entry. */
export function createLiveApp(session: WebSession): JSX.Element {
  void session.start();
  return <Live session={session} run={session.store} />;
}

function Live({session, run}: {readonly session: WebSession; readonly run: RunStore}): JSX.Element {
  const iterations = useSyncExternalStore(run.subscribe, run.getIterations, run.getIterations);
  const state = useStore(run);
  const sessionState = useStore(session);
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
      headerAction={
        <ConversationPanel
          session={session}
          runId={state.runId}
          transcript={state.chatTranscript}
          connected={sessionState.controls.status === 'connected'}
          steerAvailable={!hasRunEnded(state)}
        />
      }
    />
  );
}
