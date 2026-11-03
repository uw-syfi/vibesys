/**
 * How the app names a host: this machine, or an ssh destination. A host key is the one string that
 * crosses the window, the command line and the settings file, so every use parses it here and an
 * alias that reaches an ssh command line is validated first: it cannot start an option.
 */

/** A host the user can pick: this machine, or an ssh alias. */
export type HostId = {readonly kind: 'local'} | {readonly kind: 'ssh'; readonly alias: string};

/** `local`, or `ssh:<alias>`. */
export type HostKey = string;

export class HostValidationError extends Error {
  override name = 'HostValidationError';
}

/** An ssh alias: letters, digits, `.`, `_`, `-`, `@`, `:`; never a leading `-`. */
const SSH_ALIAS = /^[A-Za-z0-9_.@:][A-Za-z0-9_.@:-]*$/;
const MAX_ALIAS = 255;
const LOCAL_KEY: HostKey = 'local';
const SSH_PREFIX = 'ssh:';

/** `alias` when it is a usable ssh destination, else a `HostValidationError` naming it. */
export function validAlias(alias: string): string {
  if (!SSH_ALIAS.test(alias) || alias.length > MAX_ALIAS) {
    throw new HostValidationError(`"${alias}" is not a usable ssh host name`);
  }
  return alias;
}

export function hostKey(id: HostId): HostKey {
  return id.kind === 'local' ? LOCAL_KEY : `${SSH_PREFIX}${id.alias}`;
}

/** The host a key names, or a `HostValidationError` naming the key. */
export function hostFromKey(key: unknown): HostId {
  if (key === LOCAL_KEY) return {kind: 'local'};
  if (typeof key === 'string' && key.startsWith(SSH_PREFIX)) {
    return {kind: 'ssh', alias: validAlias(key.slice(SSH_PREFIX.length))};
  }
  throw new HostValidationError(`${JSON.stringify(key)} does not name a host`);
}

/** What a person reads for a host. */
export function hostLabel(id: HostId): string {
  return id.kind === 'local' ? 'This Mac' : id.alias;
}
