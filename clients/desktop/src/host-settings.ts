/**
 * Which hosts the app offers and how it runs VibeSys on each, as pure, validated data.
 *
 * Hosts are "This Mac" plus the concrete `Host` aliases of the user's `~/.ssh/config` (patterns are
 * left out: they name no one machine), or a destination the user types. Per-host settings live in
 * the app's user data (`hosts.json`), never in the repository: one VibeSys checkout per host.
 * Everything that later becomes part of an ssh command line is validated here first: an alias
 * cannot start an option, and a path is absolute (or under `~/`), one line, and never an option.
 */

/** A host the user can pick: this machine, or an SSH alias. */
export type HostId = {readonly kind: 'local'} | {readonly kind: 'ssh'; readonly alias: string};

/** `local`, or `ssh:<alias>`: how settings, recents, and the welcome view name a host. */
export type HostKey = string;

/** How the app runs VibeSys on one host. */
export interface HostSettings {
  /**
   * The VibeSys source checkout on the host (absolute, or under `~/`), e.g. `/home/me/src/vibesys`.
   * The app runs `uv run --project <checkout> vibesys` there.
   */
  readonly checkout: string;
}

export interface SettingsFile {
  readonly version: 2;
  readonly hosts: Readonly<Record<HostKey, HostSettings>>;
}

export const EMPTY_SETTINGS: SettingsFile = {version: 2, hosts: {}};

export class SettingsError extends Error {
  override name = 'SettingsError';
}

/** An ssh alias: letters, digits, `.`, `_`, `-`, `@`, `:`; never a leading `-`. */
const ALIAS = /^[A-Za-z0-9_.@:][A-Za-z0-9_.@:-]*$/;
const MAX_PATH = 1024;
const LOCAL_KEY: HostKey = 'local';
const SSH_PREFIX = 'ssh:';

/** `alias` when it is a usable ssh destination, else a `SettingsError` naming it. */
export function validAlias(alias: string): string {
  if (!ALIAS.test(alias) || alias.length > 255) {
    throw new SettingsError(`"${alias}" is not a usable ssh host name`);
  }
  return alias;
}

export function hostKey(id: HostId): HostKey {
  return id.kind === 'local' ? LOCAL_KEY : `${SSH_PREFIX}${id.alias}`;
}

/** The host a key names, or a `SettingsError` naming the key. */
export function hostFromKey(key: unknown): HostId {
  if (key === LOCAL_KEY) return {kind: 'local'};
  if (typeof key === 'string' && key.startsWith(SSH_PREFIX)) {
    return {kind: 'ssh', alias: validAlias(key.slice(SSH_PREFIX.length))};
  }
  throw new SettingsError(`${JSON.stringify(key)} does not name a host`);
}

export function hostLabel(id: HostId): string {
  return id.kind === 'local' ? 'This Mac' : id.alias;
}

/**
 * A directory path on a host (a checkout or a project), trimmed, without trailing slashes:
 * absolute or under the home directory (`~` or `~/...`), one line, no NUL, never read as an
 * option. `what` names it in the error.
 */
export function validHostPath(value: unknown, what: string): string {
  if (typeof value !== 'string') throw new SettingsError(`${what} must be a string`);
  if (/[\n\r\0]/.test(value)) throw new SettingsError(`${what} must be one line`);
  const trimmed = value.trim();
  if (trimmed === '') throw new SettingsError(`${what} must not be empty`);
  if (trimmed.startsWith('-')) throw new SettingsError(`${what} cannot start with "-"`);
  if (!(trimmed.startsWith('/') || trimmed === '~' || trimmed.startsWith('~/'))) {
    throw new SettingsError(
      `${what} must be an absolute path or start with ~/ (got ${JSON.stringify(trimmed)})`,
    );
  }
  if (trimmed.length > MAX_PATH) {
    throw new SettingsError(`${what} must be at most ${MAX_PATH} characters`);
  }
  const path = trimmed.replace(/[\s/]+$/, '');
  return path === '' ? '/' : path;
}

function object(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SettingsError(`${path} must be an object`);
  }
  return value as Record<string, unknown>;
}

function onlyKeys(record: Record<string, unknown>, keys: readonly string[], path: string): void {
  for (const key of Object.keys(record)) {
    if (!keys.includes(key)) throw new SettingsError(`${path}.${key} is not a known setting`);
  }
}

/** Validate one host's settings, rejecting unknown keys. */
function parseHostSettings(value: unknown, path: string): HostSettings {
  const record = object(value, path);
  onlyKeys(record, ['checkout'], path);
  return {checkout: validHostPath(record['checkout'], `${path}.checkout`)};
}

/**
 * Validate a whole `hosts.json` document. Version 1 stored free-form command lines, which this app
 * no longer runs: an empty one migrates, and one with hosts is rejected with what to do instead.
 */
export function parseSettings(value: unknown): SettingsFile {
  const record = object(value, 'hosts.json');
  if (record['version'] === 1) {
    onlyKeys(record, ['version', 'hosts'], 'hosts.json');
    if (Object.keys(object(record['hosts'], 'hosts.json: hosts')).length === 0) {
      return EMPTY_SETTINGS;
    }
    throw new SettingsError(
      'hosts.json is version 1, whose free-form vibesys and Python commands this app no longer ' +
        "uses. Remove the file and set each host's VibeSys checkout in the app.",
    );
  }
  if (record['version'] !== 2) throw new SettingsError('hosts.json: version must be 2');
  onlyKeys(record, ['version', 'hosts'], 'hosts.json');
  const hosts = object(record['hosts'], 'hosts.json: hosts');
  const parsed: Record<HostKey, HostSettings> = {};
  for (const [key, settings] of Object.entries(hosts)) {
    let id: HostId;
    try {
      id = hostFromKey(key);
    } catch (error) {
      throw new SettingsError(`hosts.json: hosts.${key}: ${(error as Error).message}`);
    }
    parsed[hostKey(id)] = parseHostSettings(settings, `hosts.json: hosts.${key}`);
  }
  return {version: 2, hosts: parsed};
}

export function settingsFor(file: SettingsFile, id: HostId): HostSettings | null {
  return file.hosts[hostKey(id)] ?? null;
}

export function withHostSettings(
  file: SettingsFile,
  id: HostId,
  settings: HostSettings,
): SettingsFile {
  return {
    version: 2,
    hosts: {
      ...file.hosts,
      [hostKey(id)]: {checkout: validHostPath(settings.checkout, 'the checkout')},
    },
  };
}

/**
 * The concrete host aliases of an ssh config file, in order and without duplicates. `Host` lines
 * may list several names; names with patterns (`*`, `?`, `!`) are skipped. `Include` is not
 * followed: hosts defined only in included files are not offered.
 */
export function sshConfigHosts(text: string): string[] {
  const hosts: string[] = [];
  for (const raw of text.split('\n')) {
    const line = raw.replace(/#.*/, '').trim();
    const match = /^host(?:\s*=\s*|\s+)(.+)$/i.exec(line);
    if (match === null) continue;
    for (const name of (match[1] ?? '').split(/\s+/)) {
      const unquoted = name.replace(/^"(.*)"$/, '$1');
      if (/[*?!]/.test(unquoted) || !ALIAS.test(unquoted) || hosts.includes(unquoted)) continue;
      hosts.push(unquoted);
    }
  }
  return hosts;
}
