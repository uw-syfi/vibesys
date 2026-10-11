/**
 * The browser page's entry: DOM glue around the pure boot decision (`state/boot.ts`). It scrubs
 * the launch capability from the address bar, performs the plan (remember the gateway, exchange a
 * capability), and renders the composed page.
 */
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  type GatewayConnection,
  scrubLaunchCapability,
  serializeBrowserSession,
  webSocketUrlWithSession,
} from '../entities/gateway.js';
import {WebSession} from '../features/run-view/index.js';
import {bootstrapGateway, connectRun, openTransport} from '../platform/backend.js';
import {markDesktopShell} from '../platform/desktop-shell.js';
import {GatewaySessionStore} from '../platform/gateway-storage.js';
import {browserLifecycle} from '../platform/lifecycle.js';
import '../styles.css';
import {bootPlan} from './boot.js';
import {createDemoApp, createLiveApp} from './compose.js';

// Remove the launch secret before DOM validation, storage access, or any
// asynchronous work can fail and leave it in browser-visible history.
const launchUrl = window.location.href;
const cleanUrl = scrubLaunchCapability(launchUrl);
if (cleanUrl !== launchUrl) window.history.replaceState(null, '', cleanUrl);

markDesktopShell(document.documentElement, window.vibesysDesktop);

const root = document.querySelector('#root');
if (root === null) throw new Error('Web viewer root is missing');
const reactRoot = createRoot(root);
const storedSessions = new GatewaySessionStore(() => window.sessionStorage);

function start(): void {
  const plan = bootPlan(launchUrl, storedSessions.remembered());
  if (plan.kind === 'demo') {
    renderDemo(plan.error);
    return;
  }
  storedSessions.rememberGateway(plan.target.gatewayUrl);
  if (plan.kind === 'live') {
    renderLive(plan.target.webSocketUrl, plan.browserSession);
    return;
  }
  const {target} = plan;
  bootstrapGateway(target.bootstrapUrl).then(
    browserSession => {
      storedSessions.rememberBrowserSession(
        serializeBrowserSession(target.gatewayUrl, browserSession),
      );
      renderLive(target.webSocketUrl, browserSession);
    },
    (reason: unknown) => renderDemo(reason instanceof Error ? reason.message : String(reason)),
  );
}

function gatewayConnected(connection: GatewayConnection): void {
  storedSessions.rememberGateway(connection.gatewayUrl);
  storedSessions.rememberBrowserSession(
    serializeBrowserSession(connection.gatewayUrl, connection.browserSession),
  );
  // This document already owns the freshly exchanged credential. Replacing
  // history removes the launch capability without throwing that credential
  // away on a reload when storage is unavailable.
  window.history.replaceState(null, '', connection.cleanPageUrl);
  renderLive(connection.webSocketUrl, connection.browserSession);
}

function renderDemo(initialGatewayError: string | null = null): void {
  reactRoot.render(
    <StrictMode>
      {createDemoApp({
        page: window.location.href,
        initialGatewayError,
        onGatewayConnected: gatewayConnected,
      })}
    </StrictMode>,
  );
}

function renderLive(webSocketUrl: string, browserSession: string | null): void {
  const url = webSocketUrlWithSession(webSocketUrl, browserSession);
  const session = new WebSession({
    lifecycle: browserLifecycle,
    connect: connectRun(hooks => openTransport(url, hooks)),
  });
  reactRoot.render(<StrictMode>{createLiveApp(session)}</StrictMode>);
}

start();
