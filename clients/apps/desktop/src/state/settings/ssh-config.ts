/**
 * The host aliases of an ssh config and which of them match what a person typed, as pure functions
 * of text. Only an alias that could be added is ever offered: a pattern (`*`, `?`, `!`) names no
 * one machine, and a name the app cannot hand to ssh as a destination is no alias here.
 */
import {validAlias} from '../host.js';

/** What one config file holds that matters here: the aliases it declares, and the files it pulls in. */
export interface SshConfigEntries {
  readonly aliases: readonly string[];
  readonly includes: readonly string[];
}

/** The words of an ssh config line's arguments: whitespace separated, double quotes grouping. */
function argumentsOf(rest: string): string[] {
  const out: string[] = [];
  for (const match of rest.matchAll(/"([^"]*)"|(\S+)/g)) out.push(match[1] ?? match[2] ?? '');
  return out;
}

function isAlias(name: string): boolean {
  if (/[*?!]/.test(name)) return false;
  try {
    validAlias(name);
    return true;
  } catch {
    return false;
  }
}

/** The arguments of a `Host` or `Include` line, without a trailing comment. */
const lineArguments = (rest: string | undefined): string[] =>
  argumentsOf((rest ?? '').replace(/\s#.*$/, ''));

/** The `Host` aliases and `Include` paths of one config file's text, in file order. */
export function parseSshConfig(text: string): SshConfigEntries {
  const aliases: string[] = [];
  const includes: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const split = /^([A-Za-z]+)(?:\s*=\s*|\s+)(.*)$/.exec(raw.trim());
    const keyword = split?.[1]?.toLowerCase();
    if (keyword === 'host') aliases.push(...lineArguments(split?.[2]).filter(isAlias));
    else if (keyword === 'include') includes.push(...lineArguments(split?.[2]));
  }
  return {aliases, includes};
}

/** How many matches the type-ahead offers at most. */
export const MATCH_LIMIT = 8;

/**
 * The aliases that contain `query` (ignoring case), those that start with it first, then the rest;
 * each group in the order given, each alias once, at most `limit`. An empty query matches all.
 */
export function matchAliases(
  aliases: readonly string[],
  query: string,
  limit: number = MATCH_LIMIT,
): string[] {
  const needle = query.trim().toLowerCase();
  const seen = new Set<string>();
  const prefix: string[] = [];
  const inside: string[] = [];
  for (const alias of aliases) {
    if (seen.has(alias)) continue;
    seen.add(alias);
    const hay = alias.toLowerCase();
    if (hay.startsWith(needle)) prefix.push(alias);
    else if (hay.includes(needle)) inside.push(alias);
  }
  return [...prefix, ...inside].slice(0, limit);
}
