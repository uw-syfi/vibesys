import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/500.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import '@fontsource/ibm-plex-mono/600.css';
import './tokens.css';
import './base.css';
import {BrowserBackendClient} from '@vibesys/backend-client/browser';
import {createRoot} from 'react-dom/client';
import {App} from './App.js';
import {WorkspaceSession} from './session.js';

const root = document.getElementById('root');
if (!root) throw new Error('Missing workspace root');
// One session per page load, created outside React so a remount cannot close it.
const session = new WorkspaceSession(new BrowserBackendClient());
void session.start();
createRoot(root).render(<App session={session} />);
