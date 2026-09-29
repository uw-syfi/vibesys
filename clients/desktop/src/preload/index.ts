import {contextBridge} from 'electron';

// The page's whole view of the shell: the platform drawing the window chrome, so the app can
// inset the macOS traffic lights. contextBridge hands the page a copy, so it cannot write back.
// There is no IPC channel, hence no sender to validate.
contextBridge.exposeInMainWorld('vibesysDesktop', {platform: process.platform});
