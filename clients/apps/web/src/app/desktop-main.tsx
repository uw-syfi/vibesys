import {WebSession} from '@vibesys/run-session';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {desktopBridge, desktopSessionOptions, markDesktopShell} from '../platform/desktop-shell.js';
import {browserLifecycle} from '../platform/lifecycle.js';
import '../styles.css';
import {createLiveApp} from './compose.js';
import {applySessionTheme, applyWebTheme} from './theme.js';

// The desktop shell's entry: the page is bundled inside the app, and the shell's bridge is its
// only way to the server. There is no URL, token, or gateway to read.
markDesktopShell(document.documentElement, window.vibesysDesktop);
applyWebTheme(document.documentElement, null);

const root = document.querySelector('#root');
if (root === null) throw new Error('Web viewer root is missing');
const reactRoot = createRoot(root);

const bridge = desktopBridge(window.vibesysDesktop);
if (bridge === null) {
  reactRoot.render(
    <StrictMode>
      <p role="alert">This page runs inside the VibeSys desktop app.</p>
    </StrictMode>,
  );
} else {
  const session = new WebSession(desktopSessionOptions(bridge, browserLifecycle()));
  void applySessionTheme(document.documentElement, session);
  reactRoot.render(<StrictMode>{createLiveApp(session)}</StrictMode>);
}
