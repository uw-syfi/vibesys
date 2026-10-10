/**
 * How the window loads the bundled web UI, as pure functions of URLs and paths.
 *
 * The UI ships inside the app (`dist/ui`, built by `@vibesys/web`'s `build:desktop`) and is served
 * from the privileged `app://vibesys` origin. That origin is the only one the window may navigate
 * to; it has no network access at all (`CONTENT_SECURITY_POLICY` here, and the main process cancels
 * every http(s) and ws(s) request), because the page reaches its server only through the preload
 * bridge.
 */
import {extname, isAbsolute, join, normalize, relative, sep} from 'node:path';

export const APP_SCHEME = 'app';
const APP_HOST = 'vibesys';
const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;
/** The page the window opens. */
export const APP_ENTRY_URL = `${APP_ORIGIN}/desktop.html`;

/**
 * Same-origin scripts, styles, and images only; no connections of any kind. React's inline style
 * attributes need `'unsafe-inline'` for styles, which grants no script execution.
 */
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join('; ');

const CONTENT_TYPES: Readonly<Record<string, string>> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};

/** True when the window may navigate to `url`: it is on the app's own origin. */
export function isAppPage(url: string): boolean {
  const parsed = URL.parse(url);
  return parsed !== null && parsed.protocol === `${APP_SCHEME}:` && parsed.host === APP_HOST;
}

/**
 * The file under `root` that serves `url`, or null when `url` is not an app asset: another origin,
 * an unknown file type, or a path that escapes `root` (decoded `..` or separators included).
 */
export function assetPath(url: string, root: string): string | null {
  if (!isAppPage(url)) return null;
  const parsed = new URL(url);
  let pathname: string;
  try {
    pathname = decodeURIComponent(parsed.pathname);
  } catch {
    return null;
  }
  if (pathname.includes('\0') || pathname.includes('\\')) return null;
  const file = normalize(join(root, pathname));
  const inside = relative(root, file);
  if (inside === '' || inside.startsWith('..') || isAbsolute(inside)) return null;
  if (inside.split(sep).some(part => part.startsWith('.'))) return null;
  return contentType(file) === null ? null : file;
}

/** The Content-Type for a served file, or null when the app does not serve that type. */
export function contentType(file: string): string | null {
  return CONTENT_TYPES[extname(file).toLowerCase()] ?? null;
}
