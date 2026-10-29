/**
 * The scrubber: what makes a recorded run safe to commit. It removes host names, absolute paths,
 * user names, e-mail addresses, IP addresses and the hosts of URLs from every string of a value
 * (keys included), and `findLeaks` reports what is left, so a test can assert a fixture holds none.
 *
 * Each kind of leak is one detector: a pattern, and the replacement every match gets. The
 * replacements are fixed text that no detector matches again, so scrubbing is idempotent and the
 * result of `scrub` never has a leak `findLeaks` can see.
 */

/** Names the generic detectors cannot know: the recording machine's own user and hosts. */
export interface ScrubPolicy {
  readonly users: readonly string[];
  readonly hosts: readonly string[];
}

export const NO_NAMES: ScrubPolicy = {users: [], hosts: []};

export type LeakKind =
  | 'user-name'
  | 'host-name'
  | 'ssh-host'
  | 'absolute-path'
  | 'windows-path'
  | 'email'
  | 'user-at-host'
  | 'ip-address'
  | 'url-host';

export interface Leak {
  readonly kind: LeakKind;
  /** Where in the value: `events[3].data.text`. */
  readonly path: string;
  readonly match: string;
}

interface Detector {
  readonly kind: LeakKind;
  readonly pattern: RegExp;
  replace(match: string, ...groups: string[]): string;
}

const USER = 'user';
const HOST = 'remote-host';
const EXAMPLE = 'example.com';

/** The roots an absolute path of one segment may start at (`/tmp`, `/home`). */
const ROOTS =
  'home|Users|tmp|var|etc|usr|opt|root|mnt|srv|work|workspace|data|scratch|private|nix|proj|projects';

function escaped(name: string): string {
  return name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Whole-token, case-insensitive matches of any of `names`; null when there are none. */
function named(names: readonly string[]): RegExp | null {
  const usable = names.filter(name => name.length >= 2);
  if (usable.length === 0) return null;
  const alternatives = [...usable].sort((a, b) => b.length - a.length).map(escaped);
  return new RegExp(`(?<![A-Za-z0-9_-])(?:${alternatives.join('|')})(?![A-Za-z0-9_-])`, 'gi');
}

/** The detectors for `policy`, most specific first. */
function detectors(policy: ScrubPolicy): readonly Detector[] {
  const all: Detector[] = [];
  const hosts = named(policy.hosts);
  const users = named(policy.users);
  if (hosts !== null) all.push({kind: 'host-name', pattern: hosts, replace: () => HOST});
  if (users !== null) all.push({kind: 'user-name', pattern: users, replace: () => USER});
  all.push(
    {
      kind: 'ssh-host',
      pattern: /\b(ssh|docker|slurm|host):(?!remote-host\b)[A-Za-z0-9][A-Za-z0-9._-]*/g,
      replace: (_match, scheme = 'ssh') => `${scheme}:${HOST}`,
    },
    {
      kind: 'url-host',
      pattern:
        /\b(https?|ssh|git):\/\/(?:[^\s/@:'"<>]+@)?(?!example\.com\b|localhost\b)[^\s/:'"<>]+/g,
      replace: (_match, scheme = 'https') => `${scheme}://${EXAMPLE}`,
    },
    {
      kind: 'email',
      pattern: /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b/g,
      replace: match => (match === `${USER}@${EXAMPLE}` ? match : `${USER}@${EXAMPLE}`),
    },
    {
      kind: 'user-at-host',
      pattern: /(?<![\w@/.-])[A-Za-z_][\w-]*@(?!example\.com\b)[A-Za-z][\w-]*\b(?!\.)/g,
      replace: match => (match === `${USER}@${HOST}` ? match : `${USER}@${HOST}`),
    },
    {
      kind: 'ip-address',
      pattern: /\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b/g,
      replace: match => (match === '192.0.2.1' ? match : '192.0.2.1'),
    },
    {
      kind: 'windows-path',
      pattern: /\b[A-Za-z]:\\(?:[^\\\s'"<>|]+\\)*[^\\\s'"<>|]*/g,
      replace: match => {
        const tail = /[.,:;)\]]+$/.exec(match)?.[0] ?? '';
        const base = match
          .slice(0, match.length - tail.length)
          .split('\\')
          .filter(Boolean)
          .at(-1);
        const named = base === undefined || /^[A-Za-z]:$/.test(base) || base === 'redacted';
        return `C:\\redacted\\${named ? '' : base}${tail}`;
      },
    },
    {
      kind: 'absolute-path',
      pattern: new RegExp(
        `(?<![\\w.:/~@-])(?:\\/(?:[\\w.@+%=,-]+\\/)+[\\w.@+%=,-]*|\\/(?:${ROOTS})(?![\\w.@+%=,/-]))`,
        'g',
      ),
      replace: match => {
        const base = match.split('/').filter(Boolean).at(-1) ?? '';
        return `/redacted/${base === 'redacted' ? '' : base}`.replace(/\/$/, '');
      },
    },
  );
  return all;
}

/** `text` with every leak the policy and the generic detectors know replaced. */
export function scrubText(text: string, policy: ScrubPolicy = NO_NAMES): string {
  let scrubbed = text;
  for (const detector of detectors(policy)) {
    scrubbed = scrubbed.replace(detector.pattern, (match, ...rest) =>
      detector.replace(match, ...(rest.filter(part => typeof part === 'string') as string[])),
    );
  }
  return scrubbed;
}

/** `value` with every string and every key scrubbed; the shape is unchanged. */
export function scrubValue<T>(value: T, policy: ScrubPolicy = NO_NAMES): T {
  return scrubbed(value, policy) as T;
}

function scrubbed(value: unknown, policy: ScrubPolicy): unknown {
  if (typeof value === 'string') return scrubText(value, policy);
  if (Array.isArray(value)) return value.map(item => scrubbed(item, policy));
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [scrubText(key, policy), scrubbed(item, policy)]),
    );
  }
  return value;
}

/** The leaks left in `value`: each string (and key) a detector would still change. */
export function findLeaks(value: unknown, policy: ScrubPolicy = NO_NAMES): Leak[] {
  const found: Leak[] = [];
  const list = detectors(policy);
  const visit = (item: unknown, path: string): void => {
    if (typeof item === 'string') {
      found.push(...leaksIn(item, path, list));
    } else if (Array.isArray(item)) {
      for (const [index, each] of item.entries()) visit(each, `${path}[${index}]`);
    } else if (typeof item === 'object' && item !== null) {
      for (const [key, each] of Object.entries(item)) {
        visit(key, `${path}.<key ${key}>`);
        visit(each, `${path}.${key}`);
      }
    }
  };
  visit(value, '');
  return found;
}

/** The matches in one string that a detector would change. */
function leaksIn(text: string, path: string, list: readonly Detector[]): Leak[] {
  const found: Leak[] = [];
  for (const detector of list) {
    for (const match of text.matchAll(detector.pattern)) {
      const groups = [...match].slice(1).filter(part => typeof part === 'string');
      if (detector.replace(match[0], ...groups) !== match[0]) {
        found.push({kind: detector.kind, path, match: match[0]});
      }
    }
  }
  return found;
}
