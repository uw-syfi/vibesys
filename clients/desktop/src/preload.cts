/**
 * Sandboxed preload: tells the page it runs in the desktop shell. It exposes data only, with no IPC
 * channel, so the page gains no capability. Sandboxed preloads cannot be ES modules, hence `.cts`.
 */
import electron = require('electron');

electron.contextBridge.exposeInMainWorld('vibesysDesktop', {platform: process.platform});
