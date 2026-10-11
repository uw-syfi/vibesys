/**
 * How the app names a host, and the values that later reach a host's command line, as pure,
 * validated data shared by the main process and the views.
 *
 * Hosts are "This Mac" plus ssh destinations. Everything that later becomes part of an ssh command
 * line is validated here first: an alias cannot start an option, and a path is absolute (or under
 * `~/`), one line, and never an option.
 */

/** A host the user can pick: this machine, or an SSH alias. */
export type HostId = {readonly kind: 'local'} | {readonly kind: 'ssh'; readonly alias: string};

/** `local`, or `ssh:<alias>`: how settings, recents, and the welcome view name a host. */
export type HostKey = string;

export class SettingsError extends Error {
  override name = 'SettingsError';
}

/** An ssh alias: letters, digits, `.`, `_`, `-`, `@`, `:`; never a leading `-`. */
export const SSH_ALIAS = /^[A-Za-z0-9_.@:][A-Za-z0-9_.@:-]*$/;
const MAX_PATH = 1024;
const LOCAL_KEY: HostKey = 'local';
const SSH_PREFIX = 'ssh:';

/** `alias` when it is a usable ssh destination, else a `SettingsError` naming it. */
export function validAlias(alias: string): string {
  if (!SSH_ALIAS.test(alias) || alias.length > 255) {
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

/** One host as the views show it. */
export interface HostSummary {
  readonly key: HostKey;
  readonly label: string;
  /** The saved checkout (This Mac: the app's own checkout by default); null until set. */
  readonly checkout: string | null;
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
