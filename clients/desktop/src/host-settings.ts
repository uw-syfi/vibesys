/**
 * Which hosts the app offers and how it runs VibeSys on each, as pure, validated data.
 *
 * Hosts are "This Mac" plus the concrete `Host` aliases of the user's `~/.ssh/config` (patterns are
 * left out: they name no one machine). Per-host settings live in the app's user data
 * (`hosts.json`), never in the repository. Everything that later becomes part of an ssh command
 * line is validated here first: an alias cannot start an option, and a command is one line.
 */

/** A host the user can pick: this machine, or an SSH alias. */
export type HostId = {readonly kind: 'local'} | {readonly kind: 'ssh'; readonly alias: string};

/** How the app runs VibeSys on one SSH host. */
export interface HostSettings {
  /** The command line that runs VibeSys there, e.g. `vibesys` or `uv run --project ~/src/vibesys vibesys`. */
  readonly vibesysCommand: string;
  /** The command line of the Python VibeSys runs in, when it cannot be derived. */
  readonly pythonCommand?: string;
}

export interface SettingsFile {
  readonly version: 1;
  readonly hosts: Readonly<Record<string, HostSettings>>;
}

export const DEFAULT_HOST_SETTINGS: HostSettings = {vibesysCommand: 'vibesys'};
export const EMPTY_SETTINGS: SettingsFile = {version: 1, hosts: {}};

export class SettingsError extends Error {
  override name = 'SettingsError';
}

/** An ssh alias: letters, digits, `.`, `_`, `-`, `@`, `:`; never a leading `-`. */
const ALIAS = /^[A-Za-z0-9_.@:][A-Za-z0-9_.@:-]*$/;
const MAX_COMMAND = 512;

/** `alias` when it is a usable ssh destination, else a `SettingsError` naming it. */
export function validAlias(alias: string): string {
  if (!ALIAS.test(alias) || alias.length > 255) {
    throw new SettingsError(`"${alias}" is not a usable ssh host name`);
  }
  return alias;
}

/** A command line setting, trimmed; rejected when empty, multi-line, or overlong. */
export function validCommand(value: unknown, path: string): string {
  if (typeof value !== 'string') throw new SettingsError(`${path} must be a string`);
  const command = value.trim().replace(/\s+/g, ' ');
  if (command === '') throw new SettingsError(`${path} must not be empty`);
  if (/[\n\r\0]/.test(value)) throw new SettingsError(`${path} must be one line`);
  if (command.length > MAX_COMMAND) {
    throw new SettingsError(`${path} must be at most ${MAX_COMMAND} characters`);
  }
  if (command.startsWith('-')) throw new SettingsError(`${path} must start with a command`);
  return command;
}

/** Validate one host's settings, rejecting unknown keys. */
function parseHostSettings(value: unknown, path: string): HostSettings {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SettingsError(`${path} must be an object`);
  }
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (key !== 'vibesysCommand' && key !== 'pythonCommand') {
      throw new SettingsError(`${path}.${key} is not a known setting`);
    }
  }
  const vibesysCommand = validCommand(record['vibesysCommand'], `${path}.vibesysCommand`);
  const python = record['pythonCommand'];
  return python === undefined
    ? {vibesysCommand}
    : {vibesysCommand, pythonCommand: validCommand(python, `${path}.pythonCommand`)};
}

/** Validate a whole `hosts.json` document. */
export function parseSettings(value: unknown): SettingsFile {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SettingsError('hosts.json must be an object');
  }
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (key !== 'version' && key !== 'hosts') {
      throw new SettingsError(`hosts.json: ${key} is not a known setting`);
    }
  }
  if (record['version'] !== 1) throw new SettingsError('hosts.json: version must be 1');
  const hosts = record['hosts'];
  if (typeof hosts !== 'object' || hosts === null || Array.isArray(hosts)) {
    throw new SettingsError('hosts.json: hosts must be an object');
  }
  const parsed: Record<string, HostSettings> = {};
  for (const [alias, settings] of Object.entries(hosts)) {
    parsed[validAlias(alias)] = parseHostSettings(settings, `hosts.json: hosts.${alias}`);
  }
  return {version: 1, hosts: parsed};
}

export function settingsFor(file: SettingsFile, alias: string): HostSettings {
  return file.hosts[alias] ?? DEFAULT_HOST_SETTINGS;
}

export function withHostSettings(
  file: SettingsFile,
  alias: string,
  settings: HostSettings,
): SettingsFile {
  return {version: 1, hosts: {...file.hosts, [validAlias(alias)]: settings}};
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
