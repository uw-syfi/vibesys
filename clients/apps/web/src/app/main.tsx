/**
 * The browser page's entry: DOM glue around the pure boot decision (`boot.ts`). It scrubs
 * the launch capability from the address bar, performs the plan (remember the gateway, exchange a
 * capability), and renders the composed page.
 */

import {
  type GatewayBootstrapTarget,
  type GatewayConnection,
  scrubLaunchCapability,
  serializeBrowserSession,
  webSocketUrlWithSession,
} from '@vibesys/feature-gateway-connect/renderer';
import {connectRun, WebSession} from '@vibesys/run-session';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {openTransport} from '../platform/backend.js';
import {markDesktopShell} from '../platform/desktop-shell.js';
import {GatewaySessionStore} from '../platform/gateway-storage.js';
import {browserLifecycle} from '../platform/lifecycle.js';
import '../styles.css';
import {bootPlan} from './boot.js';
import {createDemoApp, createLiveApp} from './compose.js';
import {applySessionTheme, applyWebTheme} from './theme.js';

// Remove the launch secret before DOM validation, storage access, or any
// asynchronous work can fail and leave it in browser-visible history.
const launchUrl = window.location.href;
const scrubbed = scrubLaunchCapability(launchUrl);
if (scrubbed.pageUrl !== launchUrl) {
  window.history.replaceState(scrubbed.historyState, '', scrubbed.pageUrl);
}

markDesktopShell(document.documentElement, window.vibesysDesktop);
applyWebTheme(document.documentElement, null);

const root = document.querySelector('#root');
if (root === null) throw new Error('Web viewer root is missing');
const reactRoot = createRoot(root);
const storedSessions = new GatewaySessionStore(() => window.sessionStorage);

function start(): void {
  const plan = bootPlan(launchUrl, storedSessions.remembered(), window.history.state);
  if (plan.kind === 'demo') {
    renderDemo(plan.error);
    return;
  }
  storedSessions.rememberGateway(plan.target.gatewayUrl);
  if (plan.kind === 'live') {
    renderLive(plan.target.webSocketUrl, plan.browserSession);
    return;
  }
  renderDemo(null, plan.target);
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

function renderDemo(
  initialGatewayError: string | null = null,
  initialGatewayTarget: GatewayBootstrapTarget | null = null,
): void {
  reactRoot.render(
    <StrictMode>
      {createDemoApp({
        page: window.location.href,
        initialGatewayError,
        initialGatewayTarget,
        onGatewayConnected: gatewayConnected,
      })}
    </StrictMode>,
  );
}

function renderLive(webSocketUrl: string, browserSession: string | null): void {
  const url = webSocketUrlWithSession(webSocketUrl, browserSession);
  const session = new WebSession({
    lifecycle: browserLifecycle(),
    connect: connectRun(hooks => openTransport(url, hooks)),
  });
  void applySessionTheme(document.documentElement, session);
  reactRoot.render(<StrictMode>{createLiveApp(session)}</StrictMode>);
}

start();
