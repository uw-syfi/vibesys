/**
 * Which hosts the app offers and how it runs VibeSys on each, as pure, validated data.
 *
 * Hosts are "This Mac" plus the concrete `Host` aliases of the user's `~/.ssh/config` (patterns are
 * left out: they name no one machine), or a destination the user types. Per-host settings live in
 * the app's user data (`hosts.json`), never in the repository: one VibeSys checkout per host, from
 * which the app starts the host helper.
 */
import {
  type HostId,
  type HostKey,
  hostFromKey,
  hostKey,
  SSH_ALIAS,
  validHostPath,
} from '@vibesys/hosts';
import {object, onlyKeys, SettingsError} from './parse.js';

/** How the app runs VibeSys on one host. */
export interface HostSettings {
  /**
   * The VibeSys source checkout on the host (absolute, or under `~/`), e.g. `/home/me/src/vibesys`.
   * The app starts the host helper from it (`uv run --project <checkout>`).
   */
  readonly checkout: string;
}

export interface SettingsFile {
  readonly version: 2;
  readonly hosts: Readonly<Record<HostKey, HostSettings>>;
}

export const EMPTY_SETTINGS: SettingsFile = {version: 2, hosts: {}};

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
      if (/[*?!]/.test(unquoted) || !SSH_ALIAS.test(unquoted) || hosts.includes(unquoted)) continue;
      hosts.push(unquoted);
    }
  }
  return hosts;
}
