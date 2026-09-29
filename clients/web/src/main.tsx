import './theme.css';
import {createRoot} from 'react-dom/client';
import {App} from './App.js';
import {WebSocketTransport} from './browser-entry.js';
import {fixtureHomeApi} from './home.js';
import {fetchReplay, replayTransport} from './replay.js';
import {browserLifecycle, WorkspaceSession, webSocketUrlFromLocation} from './session.js';

const root = document.getElementById('root');
if (!root) throw new Error('Missing workspace root');
const search = new URL(window.location.href).searchParams;
// System follows the OS through light-dark(); ?theme=light|dark forces one (reviews, captures).
const theme = search.get('theme');
if (theme === 'light' || theme === 'dark') document.documentElement.dataset['theme'] = theme;
const live = search.has('token') || search.has('gateway');
// One session per page load, created outside React so a remount cannot close it.
const session = new WorkspaceSession(
  live
    ? new WebSocketTransport(webSocketUrlFromLocation(window.location))
    : replayTransport(fetchReplay()),
  {lifecycle: browserLifecycle},
);
void session.start();
// The home server's API arrives with sub-project 2; until then the sidebar lists the open run.
createRoot(root).render(<App session={session} home={fixtureHomeApi()} />);
