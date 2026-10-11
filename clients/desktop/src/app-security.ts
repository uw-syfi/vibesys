/**
 * What confines the app's pages: every view is sandboxed with a preload as its only way out, stays
 * on the app's own `app://vibesys` pages, opens no window, and makes no network request; the
 * bundled pages are served with a policy that forbids every connection.
 */
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {protocol, session, type WebContents, WebContentsView} from 'electron';
import {
  APP_SCHEME,
  assetPath,
  CONTENT_SECURITY_POLICY,
  contentType,
  isAppPage,
} from './app-assets.js';

/** The one browser permission the run view uses (copying run IDs). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);
const NETWORK_URLS = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
/** `dist/ui`: the two page bundles the build copies in. */
const UI_ROOT = fileURLToPath(new URL('./ui/', import.meta.url));

/** Register `app://` as a standard, secure scheme; before the app is ready. */
export function registerAppScheme(): void {
  protocol.registerSchemesAsPrivileged([
    {scheme: APP_SCHEME, privileges: {standard: true, secure: true}},
  ]);
}

/** Keep `contents` on the app's pages and refuse every new window. */
export function confineContents(contents: WebContents, log: (message: string) => void): void {
  const stay = (event: {readonly url: string; preventDefault(): void}): void => {
    if (isAppPage(event.url)) return;
    event.preventDefault();
    log('blocked navigation away from the app');
  };
  contents.on('will-navigate', stay);
  contents.on('will-redirect', stay);
  contents.setWindowOpenHandler(() => {
    log('blocked a new window');
    return {action: 'deny'};
  });
}

/** Cancel every network request a page makes, and allow it only the clipboard. */
export function lockDownSession(log: (message: string) => void): void {
  const {defaultSession} = session;
  defaultSession.webRequest.onBeforeRequest({urls: NETWORK_URLS}, (_details, callback) => {
    log('blocked a network request');
    callback({cancel: true});
  });
  defaultSession.setPermissionRequestHandler((_contents, permission, callback) =>
    callback(ALLOWED_PERMISSIONS.has(permission)),
  );
  defaultSession.setPermissionCheckHandler((_contents, permission) =>
    ALLOWED_PERMISSIONS.has(permission),
  );
}

/** Serve the bundled pages on `app://vibesys`, with a policy that forbids every connection. */
export function serveBundledUi(): void {
  protocol.handle(APP_SCHEME, async request => {
    const file = assetPath(request.url, UI_ROOT);
    const type = file === null ? null : contentType(file);
    if (file === null || type === null) return new Response('Not found', {status: 404});
    let body: Buffer;
    try {
      body = await readFile(file);
    } catch {
      return new Response('Not found', {status: 404});
    }
    return new Response(new Uint8Array(body), {
      headers: {
        'Content-Type': type,
        'Content-Security-Policy': CONTENT_SECURITY_POLICY,
        'X-Content-Type-Options': 'nosniff',
      },
    });
  });
}

/** A sandboxed view whose only way out is `preload` (a file beside this module). */
export function sandboxedView(preload: string): WebContentsView {
  return new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
      preload: fileURLToPath(new URL(`./${preload}`, import.meta.url)),
    },
  });
}
