import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/500.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import './tokens.css';
import './base.css';
import {createRoot} from 'react-dom/client';
import {App} from './App.js';
import {WebSocketTransport} from './browser-entry.js';
import {fetchReplay, replayTransport} from './replay.js';
import {browserLifecycle, WorkspaceSession, webSocketUrlFromLocation} from './session.js';
import {GatewayConnect} from './ui/GatewayConnect.js';

const root = document.getElementById('root');
if (!root) throw new Error('Missing workspace root');
const search = new URL(window.location.href).searchParams;
const live = search.has('token') || search.has('gateway');
// One session per page load, created outside React so a remount cannot close it.
const session = new WorkspaceSession(
  live
    ? new WebSocketTransport(webSocketUrlFromLocation(window.location))
    : replayTransport(fetchReplay()),
  {lifecycle: browserLifecycle},
);
void session.start();
createRoot(root).render(<App session={session} {...(live ? {} : {connect: <GatewayConnect />})} />);
