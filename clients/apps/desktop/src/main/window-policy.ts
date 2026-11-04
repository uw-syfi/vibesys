/**
 * Which pages the app's window may show and which hosts it opens on, as pure functions of paths,
 * URLs and the command line. `index.ts` applies them to Electron.
 */
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {type HostKey, hostFromKey, hostKey} from '../state/host.js';

/** The page every window shows, inside the built UI directory. */
export const UI_PAGE = 'index.html';

/** The window's address: the built page, with one `host` parameter per host to read. */
export function windowUrl(uiDirectory: string, hosts: readonly HostKey[]): string {
  const url = pathToFileURL(join(uiDirectory, UI_PAGE));
  for (const host of hosts) url.searchParams.append('host', host);
  return url.href;
}

/** Whether `url` is a page of the built UI (any query), and not a site the window navigated to. */
export function isAppPage(uiDirectory: string, url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    parsed.protocol === 'file:' &&
    parsed.pathname === pathToFileURL(join(uiDirectory, UI_PAGE)).pathname
  );
}

/** Each distinct host key, validated, in the order given: a key that names no host is an error. */
export function distinctHosts(keys: readonly string[]): HostKey[] {
  const seen = new Set<HostKey>();
  for (const key of keys) seen.add(hostKey(hostFromKey(key)));
  return [...seen];
}

const HOST_FLAG = '--host=';

/** The hosts the command line asks for with `--host=<key>`, validated; none when it names none. */
export function hostsFromArgv(argv: readonly string[]): HostKey[] {
  return distinctHosts(
    argv
      .filter(argument => argument.startsWith(HOST_FLAG))
      .map(argument => argument.slice(HOST_FLAG.length)),
  );
}

/** This machine first, then each saved host; a command line that names hosts replaces both. */
export function startupHosts(asked: readonly HostKey[], saved: readonly string[]): HostKey[] {
  return asked.length > 0 ? [...asked] : distinctHosts(['local', ...saved]);
}
