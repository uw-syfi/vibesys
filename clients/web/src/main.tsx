import './theme.css';
import {createRoot} from 'react-dom/client';
import {App} from './App.js';
import {WebSocketTransport} from './browser-entry.js';
import {HomeWindow} from './HomeWindow.js';
import {fixtureHomeApi, type HomeApi, httpHomeApi} from './home.js';
import {HomeError, homeClient} from './home-api.js';
import {httpNotesApi, type NotesApi} from './notes.js';
import {fetchReplay, replayTransport} from './replay.js';
import {pageParams, type RunLinks, runLinks} from './route.js';
import {browserLifecycle, WorkspaceSession, webSocketUrlFromLocation} from './session.js';
import {applyTheme, initialTheme, savedTheme} from './theme.js';

const mount = document.getElementById('root');
if (!mount) throw new Error('Missing workspace root');
const root = createRoot(mount);
// System follows the OS through light-dark(); ?theme=light|dark forces one (reviews, captures).
const search = new URL(window.location.href).searchParams;
const theme = initialTheme(search.get('theme'), savedTheme());
applyTheme(document.documentElement, theme);
// The desktop shell (clients/desktop) exposes its platform; window.css insets the native chrome.
const desktop = (window as Window & {vibesysDesktop?: {platform: string}}).vibesysDesktop;
if (desktop !== undefined) document.documentElement.dataset['desktop'] = desktop.platform;
const page = pageParams(window.location.href);
const token = page.token;
const client = token === null ? null : homeClient(token, (url, init) => fetch(url, init));

/**
 * The run window: one session per page load, created outside React so a remount cannot close it.
 * `notes` is the home server's notes API, present only on a run page the home opened.
 */
const showRun = (home: HomeApi, links: RunLinks | null, notes: NotesApi | null) => {
  const live = page.token !== null || page.gateway !== null;
  // A factory, so the live transport's control channel reports its outages to
  // the session that owns them (`WorkspaceState.controls`).
  const session = new WorkspaceSession(
    hooks =>
      live
        ? new WebSocketTransport(webSocketUrlFromLocation(window.location), {
            onConnectionState: hooks.onConnectionState,
          })
        : replayTransport(fetchReplay()),
    {lifecycle: browserLifecycle},
  );
  void session.start();
  root.render(<App session={session} home={home} links={links} notes={notes} theme={theme} />);
};

if (client === null || token === null) {
  showRun(fixtureHomeApi(), null, null);
} else if (page.gateway !== null) {
  showRun(
    httpHomeApi(client, token),
    page.project === null ? null : runLinks(token, page.project),
    httpNotesApi(token),
  );
} else {
  // `?token=` alone is the home page, or a run gateway's own page (`vibesys web live`), whose
  // origin has no home API: only the home answers.
  client.projects().then(
    () => root.render(<HomeWindow client={client} token={token} theme={theme} />),
    (error: unknown) => {
      // The home answered but refused the token (a tab kept across a home restart): a run page
      // would dial a socket that can never connect, so say what to do instead.
      if (error instanceof HomeError && error.code === 'unauthorized') {
        root.render(
          <div className="win">
            <main className="main">
              <p className="empty">
                This link is no longer valid. Open the URL `vibesys web home` prints.
              </p>
            </main>
          </div>,
        );
        return;
      }
      showRun(fixtureHomeApi(), null, null);
    },
  );
}
