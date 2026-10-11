import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {createDemoApp, createLiveApp, type GatewayConnection} from './App.js';
import {markDesktopShell} from './desktop-shell.js';
import {
  bootstrapGateway,
  GatewaySessionStore,
  type GatewayTarget,
  resolveGatewayTarget,
  scrubLaunchCapability,
  webSocketUrlWithSession,
} from './gateway-session.js';
import {WebSession} from './session.js';
import './styles.css';

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

async function start(): Promise<void> {
  let target: GatewayTarget | null;
  try {
    target = resolveGatewayTarget(launchUrl, storedSessions.rememberedGateway());
  } catch (reason) {
    renderDemo(errorMessage(reason));
    return;
  }
  if (target === null) {
    renderDemo();
    return;
  }

  storedSessions.rememberGateway(target.gatewayUrl);
  let browserSession = storedSessions.browserSession(target.gatewayUrl);
  try {
    if (target.bootstrapUrl !== null) {
      browserSession = await bootstrapGateway(target.bootstrapUrl);
      storedSessions.rememberBrowserSession(target.gatewayUrl, browserSession);
    }
  } catch (reason) {
    renderDemo(errorMessage(reason));
    return;
  }
  renderLive(target.webSocketUrl, browserSession);
}

function gatewayConnected(connection: GatewayConnection): void {
  storedSessions.rememberGateway(connection.gatewayUrl);
  storedSessions.rememberBrowserSession(connection.gatewayUrl, connection.browserSession);
  // This document already owns the freshly exchanged credential. Replacing
  // history removes the launch capability without throwing that credential
  // away on a reload when storage is unavailable.
  window.history.replaceState(null, '', connection.cleanPageUrl);
  renderLive(connection.webSocketUrl, connection.browserSession);
}

function renderDemo(initialGatewayError: string | null = null): void {
  reactRoot.render(
    <StrictMode>
      {createDemoApp({initialGatewayError, onGatewayConnected: gatewayConnected})}
    </StrictMode>,
  );
}

function renderLive(webSocketUrl: string, browserSession: string | null): void {
  const session = new WebSession({
    webSocketUrl: webSocketUrlWithSession(webSocketUrl, browserSession),
  });
  reactRoot.render(<StrictMode>{createLiveApp(session)}</StrictMode>);
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

void start();
