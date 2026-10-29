import {describe, expect, test} from 'bun:test';
import {HostValidationError, hostFromKey, hostKey, validAlias, validHostPath} from '@vibesys/hosts';
import {
  EMPTY_SETTINGS,
  parseSettings,
  SshConfigSyntaxError,
  settingsFor,
  sshConfigDirectives,
  sshConfigHosts,
  sshGlobSegmentMatches,
  sshHostPatternsMatch,
  withHostSettings,
  withoutHost,
} from './host-settings.js';

function generatedStrings(alphabet: readonly string[], maximumLength: number): string[] {
  const strings = [''];
  let frontier = [''];
  for (let length = 0; length < maximumLength; length += 1) {
    frontier = frontier.flatMap(prefix => alphabet.map(character => prefix + character));
    strings.push(...frontier);
  }
  return strings;
}

function referenceWildcardMatches(pattern: string, value: string): boolean {
  const memo = new Map<string, boolean>();
  const at = (patternIndex: number, valueIndex: number): boolean => {
    const key = `${patternIndex}:${valueIndex}`;
    const known = memo.get(key);
    if (known !== undefined) return known;
    const character = pattern[patternIndex];
    let result: boolean;
    if (character === undefined) result = valueIndex === value.length;
    else if (character === '*') {
      result =
        at(patternIndex + 1, valueIndex) ||
        (valueIndex < value.length && at(patternIndex, valueIndex + 1));
    } else if (character === '?') {
      result = valueIndex < value.length && at(patternIndex + 1, valueIndex + 1);
    } else {
      result = character === value[valueIndex] && at(patternIndex + 1, valueIndex + 1);
    }
    memo.set(key, result);
    return result;
  };
  return at(0, 0);
}

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

const PIECES = ['/', '~', '-', 'a', ' ', '\n', '\0', '.', '$(x)', "'", 'é', '//', '\r'];

function arbitrary(random: () => number): string {
  return Array.from(
    {length: Math.floor(random() * 8)},
    () => PIECES[Math.floor(random() * PIECES.length)],
  ).join('');
}

describe('sshConfigHosts', () => {
  test('lists concrete aliases in order, skipping patterns, comments, and duplicates', () => {
    const config = [
      '# my hosts',
      'Host gpu-box cluster.example.org',
      '  HostName 192.0.2.10',
      'Host *.internal !bastion',
      'host=jump # inline comment',
      'Match host other',
      'Host gpu-box',
      'Host *',
      '    User someone',
      'HostName not-a-host-line',
    ].join('\n');
    expect(sshConfigHosts(config)).toEqual(['gpu-box', 'cluster.example.org', 'jump']);
  });

  test('never offers a name that ssh would read as an option', () => {
    expect(sshConfigHosts('Host -oProxyCommand=evil ok')).toEqual(['ok']);
    expect(() => validAlias('-oProxyCommand=x')).toThrow(HostValidationError);
    expect(() => validAlias('a b')).toThrow(HostValidationError);
  });

  test('returns ordered include directives without treating quoted comments as comments', () => {
    expect(
      sshConfigDirectives(
        [
          'Include config.d/*.conf "config with spaces/host #1" # comment',
          'Host "gpu-box" *.internal',
          'include=more\\ configs/*.conf',
        ].join('\n'),
      ),
    ).toEqual([
      {
        kind: 'include',
        line: 1,
        patterns: ['config.d/*.conf', 'config with spaces/host #1'],
      },
      {kind: 'host', line: 2, patterns: ['gpu-box', '*.internal'], aliases: ['gpu-box']},
      {kind: 'include', line: 3, patterns: ['more configs/*.conf']},
    ]);
  });

  test('rejects malformed quotes and empty Include arguments with a public typed error', () => {
    for (const [text, line, reason] of [
      ['Host ok\nInclude "unterminated', 2, 'invalid-quotes'],
      ['Include', 1, 'missing-argument'],
      ['Include ""', 1, 'empty-argument'],
      ['Include trailing\\', 1, 'trailing-escape'],
    ] as const) {
      try {
        sshConfigDirectives(text);
        throw new Error('malformed config unexpectedly parsed');
      } catch (error) {
        expect(error).toBeInstanceOf(SshConfigSyntaxError);
        expect(error).toMatchObject({line, reason});
      }
    }
  });

  test('applies a complete Host pattern list, including negative patterns', () => {
    expect(sshHostPatternsMatch(['gpu-*', '!gpu-bad'], 'gpu-good')).toBeTrue();
    expect(sshHostPatternsMatch(['gpu-*', '!gpu-bad'], 'gpu-bad')).toBeFalse();
    expect(sshHostPatternsMatch(['[ab]*'], 'ahost')).toBeFalse();
    expect(sshHostPatternsMatch(['[ab]*'], '[ab]host')).toBeTrue();
    expect(sshConfigHosts('Host gpu-bad !gpu-bad gpu-good')).toEqual(['gpu-good']);
  });

  test('matches only one glob path segment with OpenSSH wildcard and dot rules', () => {
    expect(sshGlobSegmentMatches('**', 'one')).toBeTrue();
    expect(sshGlobSegmentMatches('**', 'one/two')).toBeFalse();
    expect(sshGlobSegmentMatches('[ab]*', 'ahost')).toBeTrue();
    expect(sshGlobSegmentMatches('*.conf', '.hidden.conf')).toBeFalse();
    expect(sshGlobSegmentMatches('.*.conf', '.hidden.conf')).toBeTrue();
    expect(sshGlobSegmentMatches('[a-c][!x].conf', 'bz.conf')).toBeTrue();
    expect(sshGlobSegmentMatches('[a-c][!x].conf', 'bx.conf')).toBeFalse();
  });

  test('iterative wildcard matching preserves star and question-mark semantics', () => {
    const patterns = generatedStrings(['a', 'b', '*', '?'], 4);
    const values = generatedStrings(['a', 'b'], 4);
    for (const pattern of patterns) {
      for (const value of values) {
        expect(sshGlobSegmentMatches(pattern, value)).toBe(
          referenceWildcardMatches(pattern, value),
        );
      }
    }
  });
});

describe('host keys', () => {
  test('round-trip for this machine and any usable alias, including user@host', () => {
    for (const id of [
      {kind: 'local'},
      {kind: 'ssh', alias: 'gpu-box'},
      {kind: 'ssh', alias: 'me@node-1.example.org'},
    ] as const) {
      expect(hostFromKey(hostKey(id))).toEqual(id);
    }
  });

  test('reject anything else', () => {
    for (const key of ['ssh:-oProxyCommand=x', 'ssh:', 'gpu-box', 'ssh:a b', 7, null]) {
      expect(() => hostFromKey(key)).toThrow(HostValidationError);
    }
  });
});

describe('validHostPath', () => {
  test('accepts absolute and home paths, trimming whitespace and trailing slashes', () => {
    expect(validHostPath(' /home/me/src/vibesys/ ', 'p')).toBe('/home/me/src/vibesys');
    expect(validHostPath('~/src/vibesys', 'p')).toBe('~/src/vibesys');
    expect(validHostPath('~', 'p')).toBe('~');
    expect(validHostPath('/', 'p')).toBe('/');
  });

  test('names what is wrong', () => {
    const cases: [unknown, string][] = [
      ['', 'p must not be empty'],
      ['src/vibesys', 'p must be an absolute path or start with ~/'],
      ['~me/src', 'p must be an absolute path or start with ~/'],
      ['-oProxyCommand=x', 'p cannot start with "-"'],
      ['/a\n/b', 'p must be one line'],
      ['/a\0', 'p must be one line'],
      [3, 'p must be a string'],
    ];
    for (const [value, message] of cases) expect(() => validHostPath(value, 'p')).toThrow(message);
  });

  test('every accepted path is one line, absolute or under ~, and never an option', () => {
    const random = generator(20_261_010);
    for (let round = 0; round < 2000; round += 1) {
      const value = arbitrary(random);
      let path: string;
      try {
        path = validHostPath(value, 'p');
      } catch (error) {
        expect(error).toBeInstanceOf(HostValidationError);
        continue;
      }
      expect(path).not.toMatch(/[\n\r\0]/);
      expect(path.startsWith('-')).toBe(false);
      expect(path.startsWith('/') || path === '~' || path.startsWith('~/')).toBe(true);
      expect(validHostPath(path, 'p')).toBe(path);
    }
  });
});

describe('host settings', () => {
  test('a host without settings has no checkout', () => {
    expect(settingsFor(EMPTY_SETTINGS, {kind: 'ssh', alias: 'gpu-box'})).toBeNull();
  });

  test('settings round-trip through JSON', () => {
    const file = withHostSettings(
      withHostSettings(EMPTY_SETTINGS, {kind: 'ssh', alias: 'gpu-box'}, {checkout: '~/vibesys/'}),
      {kind: 'local'},
      {checkout: '/Users/me/src/vibesys'},
    );
    expect(file.hosts).toEqual({
      'ssh:gpu-box': {checkout: '~/vibesys'},
      local: {checkout: '/Users/me/src/vibesys'},
    });
    expect(parseSettings(JSON.parse(JSON.stringify(file)))).toEqual(file);
  });

  test('removing a host keeps every other host and round-trips through JSON', () => {
    const ids = [
      {kind: 'local'},
      {kind: 'ssh', alias: 'gpu-box'},
      {kind: 'ssh', alias: 'host-b'},
    ] as const;
    for (const removed of ids) {
      for (let mask = 0; mask < 1 << ids.length; mask++) {
        const kept = ids.filter((_, index) => (mask & (1 << index)) !== 0);
        const file = kept.reduce(
          (each, id) => withHostSettings(each, id, {checkout: `~/vibesys-${hostKey(id)}`}),
          EMPTY_SETTINGS,
        );
        const after = withoutHost(file, removed);
        expect(settingsFor(after, removed)).toBeNull();
        for (const id of kept.filter(each => hostKey(each) !== hostKey(removed))) {
          expect(settingsFor(after, id)).toEqual(settingsFor(file, id));
        }
        expect(parseSettings(JSON.parse(JSON.stringify(after)))).toEqual(after);
      }
    }
  });

  test('an empty version 1 file migrates; one with commands is rejected with what to do', () => {
    expect(parseSettings({version: 1, hosts: {}})).toEqual(EMPTY_SETTINGS);
    expect(() =>
      parseSettings({version: 1, hosts: {'gpu-box': {vibesysCommand: 'vibesys'}}}),
    ).toThrow("set each host's VibeSys checkout in the app");
  });

  test('rejects unknown keys and unusable values, naming the path', () => {
    const cases: [unknown, string][] = [
      [{version: 3, hosts: {}}, 'version must be 2'],
      [{version: 2, hosts: {}, extra: 1}, 'hosts.json.extra is not a known field'],
      [{version: 1, hosts: {}, extra: 1}, 'hosts.json.extra is not a known field'],
      [{version: 2, hosts: {local: {checkout: '/x', shell: 'y'}}}, 'hosts.local.shell'],
      [{version: 2, hosts: {'ssh:a': {vibesysCommand: 'x'}}}, 'hosts.ssh:a.vibesysCommand'],
      [{version: 2, hosts: {'ssh:a': {checkout: 'rel'}}}, 'hosts.ssh:a.checkout must be an'],
      [{version: 2, hosts: {'ssh:-x': {checkout: '/x'}}}, 'not a usable ssh host name'],
      [{version: 2, hosts: {gpu: {checkout: '/x'}}}, 'does not name a host'],
      [{version: 2, hosts: []}, 'hosts must be an object'],
    ];
    for (const [value, message] of cases) {
      expect(() => parseSettings(value)).toThrow(message);
    }
  });
});
