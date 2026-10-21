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
import {sshHostPatternsMatch} from './ssh-pattern.js';

export {sshGlobSegmentMatches, sshHostPatternsMatch} from './ssh-pattern.js';

/** How the app runs VibeSys on one host. */
export interface HostSettings {
  /**
   * The VibeSys source checkout on the host (absolute, or under `~/`), e.g. `/home/me/src/vibesys`.
   * The app starts the host helper from it (`uv run --project <checkout>`).
   */
  readonly checkout: string;
  readonly runTransportV2?: boolean;
}

export interface SettingsFile {
  readonly version: 2;
  readonly hosts: Readonly<Record<HostKey, HostSettings>>;
}

export const EMPTY_SETTINGS: SettingsFile = {version: 2, hosts: {}};

/** Validate one host's settings, rejecting unknown keys. */
function parseHostSettings(value: unknown, path: string): HostSettings {
  const record = object(value, path);
  onlyKeys(record, ['checkout', 'runTransportV2'], path);
  if (record['runTransportV2'] !== undefined && typeof record['runTransportV2'] !== 'boolean') {
    throw new SettingsError(`${path}.runTransportV2 must be a boolean`);
  }
  return {
    checkout: validHostPath(record['checkout'], `${path}.checkout`),
    ...(record['runTransportV2'] === undefined
      ? {}
      : {runTransportV2: record['runTransportV2'] as boolean}),
  };
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
      [hostKey(id)]: parseHostSettings(settings, 'the host'),
    },
  };
}

/** `file` without `id`'s settings: the app no longer runs VibeSys there. */
export function withoutHost(file: SettingsFile, id: HostId): SettingsFile {
  const key = hostKey(id);
  return {
    version: 2,
    hosts: Object.fromEntries(Object.entries(file.hosts).filter(([each]) => each !== key)),
  };
}

/** One discovery-relevant directive in an OpenSSH client configuration, in source order. */
export type SshConfigDirective =
  | {
      readonly kind: 'host';
      readonly line: number;
      readonly patterns: readonly string[];
      readonly aliases: readonly string[];
    }
  | {readonly kind: 'match'; readonly line: number; readonly conditions: readonly string[]}
  | {readonly kind: 'include'; readonly line: number; readonly patterns: readonly string[]};

export type SshConfigSyntaxReason =
  | 'empty-argument'
  | 'invalid-quotes'
  | 'missing-argument'
  | 'trailing-escape';

/** A malformed OpenSSH configuration line, before any filesystem traversal occurs. */
export class SshConfigSyntaxError extends Error {
  override readonly name = 'SshConfigSyntaxError';

  constructor(
    readonly line: number,
    readonly directive: string,
    readonly reason: SshConfigSyntaxReason,
  ) {
    super(`SSH config line ${line}: ${directive} has ${syntaxReason(reason)}`);
  }
}

/**
 * Parse only the `Host` and `Include` directives needed by host discovery. This is deliberately
 * pure: filesystem expansion and recursion belong to the desktop main-process adapter.
 */
export function sshConfigDirectives(text: string): readonly SshConfigDirective[] {
  const directives: SshConfigDirective[] = [];
  for (const [index, raw] of text.split('\n').entries()) {
    const lineNumber = index + 1;
    const line = sshConfigLine(raw);
    if (line === null) continue;
    const match = /^([a-z][a-z0-9-]*)(.*)$/i.exec(line);
    if (match === null) continue;
    const directive = match[1] ?? '';
    const remainder = match[2] ?? '';
    const argumentsText = remainder.trimStart().startsWith('=')
      ? remainder.trimStart().slice(1).trimStart()
      : remainder.trimStart();
    const words = sshConfigWords(argumentsText, lineNumber, directive);
    switch (directive.toLowerCase()) {
      case 'host': {
        requireSshArguments(words, lineNumber, directive);
        const aliases = words.filter(
          name => !name.startsWith('!') && !/[*?]/.test(name) && SSH_ALIAS.test(name),
        );
        directives.push({kind: 'host', line: lineNumber, patterns: words, aliases});
        break;
      }
      case 'include': {
        requireSshArguments(words, lineNumber, directive);
        if (words.some(word => word === '')) {
          throw new SshConfigSyntaxError(lineNumber, directive, 'empty-argument');
        }
        directives.push({kind: 'include', line: lineNumber, patterns: words});
        break;
      }
      case 'match':
        requireSshArguments(words, lineNumber, directive);
        directives.push({kind: 'match', line: lineNumber, conditions: words});
        break;
    }
  }
  return Object.freeze(directives);
}

/** The concrete aliases declared directly in one config file, in order and without duplicates. */
export function sshConfigHosts(text: string): string[] {
  const hosts: string[] = [];
  for (const directive of sshConfigDirectives(text)) {
    if (directive.kind !== 'host') continue;
    for (const alias of directive.aliases) {
      if (sshHostPatternsMatch(directive.patterns, alias) && !hosts.includes(alias)) {
        hosts.push(alias);
      }
    }
  }
  return hosts;
}

/** Remove an unquoted comment while retaining quoted `#` and escaped characters. */
function sshConfigLine(raw: string): string | null {
  let quoted = false;
  let escaped = false;
  let line = '';
  for (const character of raw) {
    if (escaped) {
      line += character;
      escaped = false;
      continue;
    }
    escaped = character === '\\';
    const nextQuote = quoteAfter(character, quoted);
    if (nextQuote !== undefined) quoted = nextQuote;
    if (character === '#' && !quoted) break;
    line += character;
  }
  const trimmed = line.trim();
  return trimmed === '' ? null : trimmed;
}

/** Split the argument half of a directive using OpenSSH's double-quote and backslash rules. */
function sshConfigWords(value: string, line: number, directive: string): string[] {
  const words: string[] = [];
  let cursor = 0;
  while (cursor < value.length) {
    while (/\s/.test(value[cursor] ?? '')) cursor += 1;
    if (cursor >= value.length) break;
    const parsed = sshConfigWord(value, cursor, line, directive);
    words.push(parsed.value);
    cursor = parsed.next;
  }
  return words;
}

function sshConfigWord(
  input: string,
  start: number,
  line: number,
  directive: string,
): {readonly value: string; readonly next: number} {
  let value = '';
  let cursor = start;
  let quoted = false;
  while (cursor < input.length) {
    const character = input[cursor] ?? '';
    if (character === '\\') {
      const escaped = escapedSshCharacter(input, cursor, line, directive);
      value += escaped.value;
      cursor = escaped.index;
    } else {
      const nextQuote = quoteAfter(character, quoted);
      if (nextQuote !== undefined) quoted = nextQuote;
      else if (/\s/.test(character) && !quoted) break;
      else value += character;
    }
    cursor += 1;
  }
  if (quoted) throw new SshConfigSyntaxError(line, directive, 'invalid-quotes');
  return {value, next: cursor};
}

function escapedSshCharacter(
  input: string,
  escapeIndex: number,
  line: number,
  directive: string,
): {readonly index: number; readonly value: string} {
  const index = escapeIndex + 1;
  const value = input[index];
  if (value === undefined) {
    throw new SshConfigSyntaxError(line, directive, 'trailing-escape');
  }
  return {index, value};
}

function quoteAfter(character: string, quoted: boolean): boolean | undefined {
  return character === '"' ? !quoted : undefined;
}

function requireSshArguments(words: readonly string[], line: number, directive: string): void {
  if (words.length === 0) {
    throw new SshConfigSyntaxError(line, directive, 'missing-argument');
  }
}

function syntaxReason(reason: SshConfigSyntaxReason): string {
  switch (reason) {
    case 'empty-argument':
      return 'an empty argument';
    case 'invalid-quotes':
      return 'invalid quotes';
    case 'missing-argument':
      return 'no argument';
    case 'trailing-escape':
      return 'a trailing escape';
  }
}
