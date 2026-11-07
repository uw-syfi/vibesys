/**
 * What an ssh command names, read from text and never run: a destination and the few options a
 * machine needs (`-p`, `-l`, `-J`, `-i` and the `-o` keys that say the same). The set is closed: a
 * flag outside it is refused by name, so nothing the person pastes can reach ssh as an option the
 * app did not mean. A target is the one description of how to reach a machine: the saved host's key
 * is made from its destination (`ssh:[user@]host`), `sshArgs` turns the rest into the flags ssh
 * takes, and `printSshCommand` reads back as the same target.
 */
import {HostValidationError, validAlias} from '../host.js';

/** A machine by ssh: its destination, and the options beyond it that a pasted command carried. */
export interface SshTarget {
  /** `host` or `user@host`: what ssh takes as its destination, valid as a host alias. */
  readonly destination: string;
  readonly port?: number;
  /** A ProxyJump list: `[user@]host[:port]`, comma separated. */
  readonly jump?: string;
  readonly identityFile?: string;
}

/** The text is not an ssh command this app accepts; the message names the offending part. */
export class SshCommandError extends Error {
  override name = 'SshCommandError';
}

/** A jump list: hosts joined by commas, never starting with a dash. Held to the proto's rule by a test. */
export const JUMP_PATTERN = /^[A-Za-z0-9_.@:[][A-Za-z0-9_.@:,[\]-]*$/;
const MAX_JUMP = 512;
/** An identity file path: one line, never starting with a dash. Held to the proto's rule by a test. */
const IDENTITY_PATTERN = /^[^\0\r\n-][^\0\r\n]*$/;
const MAX_IDENTITY = 1024;
/** The user of a destination. */
const USER_PATTERN = /^[A-Za-z0-9_.][A-Za-z0-9_.-]*$/;
/** A host name or address of a destination, without its user. */
const HOST_PATTERN = /^[A-Za-z0-9_.:][A-Za-z0-9_.:-]*$/;

/**
 * One piece of a command line: blanks, a double-quoted string, a single-quoted one, a backslash
 * escape, shell syntax (refused), or plain characters. A quote that never closes matches none.
 */
const PIECE =
  /([ \t]+)|"((?:[^"\\]|\\[\s\S])*)"|'([^']*)'|\\([\s\S])|([;|&<>()$`\r\n])|([^\s"'\\;|&<>()$`]+)/y;

/** What a matched piece adds to the word being read; shell syntax is an error, blanks add nothing. */
function pieceText(match: RegExpExecArray): string {
  const [, , doubled, single, escaped, syntax, plain] = match;
  if (syntax !== undefined) throw new SshCommandError(`shell syntax "${syntax}" is not accepted`);
  return doubled?.replace(/\\([\s\S])/g, '$1') ?? single ?? escaped ?? plain ?? '';
}

/** The words of `text` as a shell reads quotes and backslashes; shell syntax outside quotes is an error. */
function words(text: string): string[] {
  const out: string[] = [];
  let current: string | null = null;
  PIECE.lastIndex = 0;
  while (PIECE.lastIndex < text.length) {
    const match = PIECE.exec(text);
    if (match === null) throw new SshCommandError('a quote is not closed');
    if (match[1] === undefined) current = (current ?? '') + pieceText(match);
    else if (current !== null) {
      out.push(current);
      current = null;
    }
  }
  if (current !== null) out.push(current);
  return out;
}

/** The option letters that take a value; any other letter is refused. */
const VALUED_FLAGS = new Set(['p', 'l', 'J', 'i', 'o']);

/** `-o` keys that say what a flag says, by the lower-cased key. */
const OPTION_KEYS: Readonly<Record<string, 'p' | 'l' | 'J' | 'i'>> = {
  port: 'p',
  user: 'l',
  proxyjump: 'J',
  identityfile: 'i',
};

interface Collected {
  p?: string;
  l?: string;
  J?: string;
  i?: string;
}

function setFlag(found: Collected, flag: keyof Collected, value: string, shown: string): void {
  if (found[flag] !== undefined) throw new SshCommandError(`${shown} is given twice`);
  found[flag] = value;
}

/** `-o Key=value` or `-o "Key value"` for a key of the closed set; any other key is refused by name. */
function setOption(found: Collected, option: string): void {
  const split = /^([A-Za-z]+)(?:\s*=\s*|\s+)(.*)$/.exec(option.trim());
  const key = split?.[1];
  const flag = key === undefined ? undefined : OPTION_KEYS[key.toLowerCase()];
  if (flag === undefined || split?.[2] === undefined || split[2] === '') {
    throw new SshCommandError(`-o ${option.split(/[=\s]/)[0] ?? option} is not accepted`);
  }
  setFlag(found, flag, split[2], `-o ${key}`);
}

function portOf(text: string): number {
  if (!/^[0-9]{1,5}$/.test(text) || Number(text) < 1 || Number(text) > 65535) {
    throw new SshCommandError(`-p ${text} is not a port from 1 to 65535`);
  }
  return Number(text);
}

function destinationOf(raw: string, user: string | undefined): string {
  const at = raw.lastIndexOf('@');
  const host = at < 0 ? raw : raw.slice(at + 1);
  const own = at < 0 ? undefined : raw.slice(0, at);
  if (!HOST_PATTERN.test(host) || (own !== undefined && !USER_PATTERN.test(own))) {
    throw new SshCommandError(`"${raw}" is not a destination`);
  }
  if (own !== undefined && user !== undefined) {
    throw new SshCommandError(`-l ${user} conflicts with the user in ${raw}`);
  }
  const named = own ?? user;
  if (named !== undefined && !USER_PATTERN.test(named)) {
    throw new SshCommandError(`-l ${named} is not a user name`);
  }
  try {
    return validAlias(named === undefined ? host : `${named}@${host}`);
  } catch (error) {
    throw new SshCommandError(error instanceof HostValidationError ? error.message : String(error));
  }
}

/** Reads the flag at `rest[at]` into `found`; the index of the last word it used. */
function takeFlag(found: Collected, rest: readonly string[], at: number): number {
  const word = rest[at] ?? '';
  const letter = word.charAt(1);
  if (!VALUED_FLAGS.has(letter)) throw new SshCommandError(`-${letter} is not accepted`);
  const attached = word.slice(2);
  const value = attached === '' ? (rest[at + 1] ?? '') : attached;
  if (value === '') throw new SshCommandError(`-${letter} needs a value`);
  if (letter === 'o') setOption(found, value);
  else setFlag(found, letter as keyof Collected, value, `-${letter}`);
  return attached === '' ? at + 1 : at;
}

/** The flags and the positional words of the command's arguments; a flag outside the set is refused. */
function collect(rest: readonly string[]): {found: Collected; positional: string[]} {
  const found: Collected = {};
  const positional: string[] = [];
  for (let i = 0; i < rest.length; i += 1) {
    const word = rest[i] ?? '';
    if (word === '--') {
      positional.push(...rest.slice(i + 1));
      break;
    }
    if (word.startsWith('-') && word !== '-') i = takeFlag(found, rest, i);
    else positional.push(word);
  }
  return {found, positional};
}

/** `value` when it is a jump list or an identity file as the contract holds them, else an error naming the flag. */
function checked(flag: string, value: string, max: number, pattern: RegExp, what: string): string {
  if (value.length > max || !pattern.test(value)) {
    throw new SshCommandError(`${flag} ${value} is not ${what}`);
  }
  return value;
}

/**
 * The target a pasted `ssh ...` command names. Refuses, naming it, any flag outside `-p -l -J -i
 * -o` (and `-o` keys outside Port, User, ProxyJump, IdentityFile), a flag given twice, shell
 * syntax, a remote command and a missing destination.
 */
export function parseSshCommand(text: string): SshTarget {
  const [program, ...rest] = words(text);
  if (program !== 'ssh') throw new SshCommandError('not an ssh command');
  const {found, positional} = collect(rest);
  const [destination, ...remote] = positional;
  if (destination === undefined) throw new SshCommandError('the command has no destination');
  if (remote.length > 0)
    throw new SshCommandError(`a remote command (${remote[0]}) is not accepted`);
  return {
    destination: destinationOf(destination, found.l),
    ...(found.p === undefined ? {} : {port: portOf(found.p)}),
    ...(found.J === undefined
      ? {}
      : {jump: checked('-J', found.J, MAX_JUMP, JUMP_PATTERN, 'a jump host list')}),
    ...(found.i === undefined
      ? {}
      : {
          identityFile: checked('-i', found.i, MAX_IDENTITY, IDENTITY_PATTERN, 'an identity file'),
        }),
  };
}

/** The target an entry names: an alias is a command-less target (its options live in the person's ssh config). */
export function targetFromInput(input: string): SshTarget {
  const text = input.trim();
  if (text === '') throw new SshCommandError('type a host or paste an ssh command');
  if (/\s/.test(text)) return parseSshCommand(text);
  try {
    return {destination: validAlias(text)};
  } catch (error) {
    throw new SshCommandError(error instanceof HostValidationError ? error.message : String(error));
  }
}

/** The flags ssh takes for the target's options, one argument each; the destination is not among them. */
export function sshArgs(target: SshTarget): string[] {
  const args: string[] = [];
  if (target.port !== undefined) args.push('-p', String(target.port));
  if (target.jump !== undefined) args.push('-J', target.jump);
  if (target.identityFile !== undefined) args.push('-i', target.identityFile);
  return args;
}

const quoted = (word: string): string =>
  /^[A-Za-z0-9_@%+=:,./~[\]-]+$/.test(word) ? word : `'${word.replaceAll("'", `'\\''`)}'`;

/** The command a person would type for the target; `parseSshCommand` reads it back as the same target. */
export function printSshCommand(target: SshTarget): string {
  return ['ssh', ...sshArgs(target).map(quoted), quoted(target.destination)].join(' ');
}
