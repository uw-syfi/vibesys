import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {createDemoApp, createLiveApp} from './App.js';
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

const root = document.querySelector('#root');
if (root === null) throw new Error('Web viewer root is missing');
const reactRoot = createRoot(root);

async function start(): Promise<void> {
  const storedSessions = new GatewaySessionStore(() => window.sessionStorage);
  let target: GatewayTarget | null;
  try {
    target = resolveGatewayTarget(launchUrl, storedSessions.rememberedGateway());
  } catch (reason) {
    reactRoot.render(<StrictMode>{createDemoApp(errorMessage(reason))}</StrictMode>);
    return;
  }
  if (target === null) {
    reactRoot.render(<StrictMode>{createDemoApp()}</StrictMode>);
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
    reactRoot.render(<StrictMode>{createDemoApp(errorMessage(reason))}</StrictMode>);
    return;
  }
  const session = new WebSession({
    webSocketUrl: webSocketUrlWithSession(target.webSocketUrl, browserSession),
  });
  reactRoot.render(<StrictMode>{createLiveApp(session)}</StrictMode>);
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

void start();
