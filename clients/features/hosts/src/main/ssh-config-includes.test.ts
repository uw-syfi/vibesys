import {describe, expect, test} from 'bun:test';
import {basename, dirname} from 'node:path';
import {ConnectionManager} from '@vibesys/connection';
import {idleRunWatch} from '@vibesys/connection/testing';
import {SettingsStore, SshConfigSyntaxError} from '@vibesys/settings';
import {FakeClock} from '@vibesys/transport/testing';
import {Hosts, SshConfigDiscoveryError, type SshConfigFilesystem} from './index.js';

const HOME = '/home/test';
const ROOT = `${HOME}/.ssh/config`;

class FakeSshConfigFilesystem implements SshConfigFilesystem {
  readonly reads: string[] = [];

  constructor(
    readonly files: Readonly<Record<string, string>>,
    readonly canonical: Readonly<Record<string, string>> = {},
    readonly extraEntries: Readonly<Record<string, readonly string[]>> = {},
  ) {}

  async readText(path: string, maximumBytes: number): Promise<string> {
    this.reads.push(path);
    const text = this.files[path];
    if (text === undefined) throw fileError('ENOENT', path);
    if (Buffer.byteLength(text, 'utf8') > maximumBytes) {
      throw new Error('SSH config includes exceed the 1048576-byte traversal limit');
    }
    return text;
  }

  async realPath(path: string): Promise<string> {
    const canonical = this.canonical[path] ?? path;
    if (this.files[canonical] === undefined) throw fileError('ENOENT', path);
    return canonical;
  }

  async *directoryEntries(path: string): AsyncIterable<string> {
    const entries = new Set(this.extraEntries[path] ?? []);
    for (const candidate of [
      ...Object.keys(this.files),
      ...Object.keys(this.canonical),
      ...Object.keys(this.extraEntries),
    ]) {
      let descendant = candidate;
      while (dirname(descendant) !== descendant) {
        if (dirname(descendant) === path) entries.add(basename(descendant));
        descendant = dirname(descendant);
      }
    }
    for (const entry of entries) yield entry;
  }
}

function fileError(code: string, path: string): Error {
  return Object.assign(new Error(`${code}: ${path}`), {code});
}

/** The public feature over a deterministic config graph; discovery must never open a connection. */
function subject(filesystem: SshConfigFilesystem): Hosts {
  return new Hosts({
    runs: idleRunWatch(),
    manager: new ConnectionManager({
      linkFor: () => {
        throw new Error('alias discovery must not open a host connection');
      },
      appVersion: 'test',
      scheduleTimeout: new FakeClock().scheduleTimeout,
      log: () => {},
    }),
    settings: new SettingsStore({
      settingsPath: '/unused/hosts.json',
      recentPath: '/unused/recent.json',
      preferencesPath: '/unused/preferences.json',
      log: () => {},
    }),
    localCheckout: '/vibesys',
    sshConfigPath: ROOT,
    sshConfigFilesystem: filesystem,
    sshHomePath: HOME,
    log: () => {},
    push: () => {},
    retire: () => {},
  });
}

describe('Hosts.aliases OpenSSH includes', () => {
  test('discovers relative includes inline and expands matches in lexical order', async () => {
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: ['Host before', 'Match all', 'Include config.d/*.conf', 'Host after'].join('\n'),
      [`${HOME}/.ssh/config.d/a.conf`]: 'Host alpha before',
      [`${HOME}/.ssh/config.d/z.conf`]: 'Host zulu',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual([
      'before',
      'alpha',
      'zulu',
      'after',
    ]);
    expect(filesystem.reads).toEqual([
      ROOT,
      `${HOME}/.ssh/config.d/a.conf`,
      `${HOME}/.ssh/config.d/z.conf`,
    ]);
  });

  test('resolves home and absolute patterns and visits one canonical file only once', async () => {
    const shared = `${HOME}/.ssh/shared/hosts.conf`;
    const alias = `${HOME}/.ssh/shared/alias.conf`;
    const system = '/etc/ssh/desktop.conf';
    const filesystem = new FakeSshConfigFilesystem(
      {
        [ROOT]: ['Include ~/.ssh/shared/*.conf', 'Include /etc/ssh/desktop.conf', 'Host root'].join(
          '\n',
        ),
        [shared]: 'Host shared\nInclude config',
        [system]: 'Host system shared',
      },
      {[alias]: shared},
    );

    await expect(subject(filesystem).aliases()).resolves.toEqual(['shared', 'system', 'root']);
    expect(filesystem.reads).toEqual([ROOT, shared, system]);
  });

  test('bounds a previously unseen include chain instead of recursing forever', async () => {
    const files: Record<string, string> = {[ROOT]: 'Include chain/0'};
    for (let index = 0; index < 18; index += 1) {
      const path = `${HOME}/.ssh/chain/${index}`;
      files[path] = `Host host-${index}\nInclude chain/${index + 1}`;
    }
    files[`${HOME}/.ssh/chain/18`] = 'Host too-deep';

    await expect(subject(new FakeSshConfigFilesystem(files)).aliases()).rejects.toThrow(
      '16-level traversal limit',
    );
  });

  test('bounds bytes before a config filesystem can return an oversized document', async () => {
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: `#${'x'.repeat(1024 * 1024)}`,
    });

    await expect(subject(filesystem).aliases()).rejects.toThrow('1048576-byte traversal limit');
  });

  test('does not invent aliases from an Include beneath an inactive Host', async () => {
    const global = `${HOME}/.ssh/global.conf`;
    const child = `${HOME}/.ssh/scoped.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: 'Include global.conf\nHost outer\n  Include scoped.conf',
      [global]: 'Host visible',
      [child]: 'Host hidden',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual(['visible', 'outer']);
  });

  test('intersects nested Host gates and treats Match all differently from unknown Match', async () => {
    const conditional = `${HOME}/.ssh/conditional.conf`;
    const hidden = `${HOME}/.ssh/hidden.conf`;
    const global = `${HOME}/.ssh/global.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: [
        'Host gpu-*',
        '  Include conditional.conf',
        'Match user someone',
        '  Include hidden.conf',
        'Match all',
        '  Include global.conf',
      ].join('\n'),
      [conditional]: 'Host gpu-1 cpu-1',
      [hidden]: 'Host match-hidden',
      [global]: 'Host visible',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual(['gpu-1', 'visible']);
  });

  test('does not evaluate Match host against the original alias after HostName changes it', async () => {
    const conditional = `${HOME}/.ssh/match-host.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: [
        'Host gpu-*',
        '  HostName actual.example',
        'Match host gpu-*',
        '  Include match-host.conf',
      ].join('\n'),
      [conditional]: 'Host gpu-prod',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual([]);
  });

  test('evaluates Match originalhost pattern lists against aliases without case sensitivity', async () => {
    const global = `${HOME}/.ssh/global.conf`;
    const conditional = `${HOME}/.ssh/match-originalhost.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: [
        'Include global.conf',
        'Match OrIgInAlHoSt GPU-*,!gpu-BAD',
        '  Include match-originalhost.conf',
      ].join('\n'),
      [global]: 'Host visible',
      [conditional]: 'Host gpu-box GPU-BAD GPU-UPPER cpu-box',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual([
      'visible',
      'gpu-box',
      'GPU-UPPER',
    ]);
  });

  test('fails closed for Match predicates that need connection state or unknown semantics', async () => {
    const opaqueConditions = [
      'host gpu-*',
      'originalhost gpu-* user someone',
      'exec "true"',
      'user someone',
      'localnetwork 10.0.0.0/8',
      'canonical',
      'final',
      'unknown gpu-*',
    ];

    for (const conditions of opaqueConditions) {
      const conditional = `${HOME}/.ssh/opaque.conf`;
      const filesystem = new FakeSshConfigFilesystem({
        [ROOT]: `Match ${conditions}\n  Include opaque.conf`,
        [conditional]: 'Host gpu-prod',
      });

      expect({conditions, aliases: await subject(filesystem).aliases()}).toEqual({
        conditions,
        aliases: [],
      });
    }
  });

  test('does not interpret bracket expressions as OpenSSH Host wildcards', async () => {
    const global = `${HOME}/.ssh/global.conf`;
    const conditional = `${HOME}/.ssh/bracket-host.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: 'Include global.conf\nHost [ab]*\n  Include bracket-host.conf',
      [global]: 'Host visible',
      [conditional]: 'Host ahost',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual(['visible']);
  });

  test('re-evaluates a cached file when a later Include has an active gate', async () => {
    const shared = `${HOME}/.ssh/shared.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: ['Host outer', '  Include shared.conf', 'Match all', '  Include shared.conf'].join(
        '\n',
      ),
      [shared]: 'Host shared',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual(['outer', 'shared']);
    expect(filesystem.reads.filter(path => path === shared)).toHaveLength(1);
  });

  test('treats double star as one path segment instead of recursive globstar', async () => {
    const direct = `${HOME}/.ssh/tree/one/child.conf`;
    const deep = `${HOME}/.ssh/tree/one/two/child.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: 'Include tree/**/child.conf',
      [direct]: 'Host direct',
      [deep]: 'Host deep',
    });

    await expect(subject(filesystem).aliases()).resolves.toEqual(['direct']);
  });

  test('evaluates a long valid Include pattern without leaking a matcher RangeError', async () => {
    const included = `${HOME}/.ssh/hosts.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: `Include ${'*'.repeat(50_000)}`,
      [included]: 'Host included',
    });

    const aliases = await subject(filesystem).aliases();
    expect(aliases).toEqual(['included']);
  });

  test('surfaces malformed included syntax as a typed path-specific discovery error', async () => {
    const malformed = `${HOME}/.ssh/malformed.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: 'Include malformed.conf',
      [malformed]: 'Host "unterminated',
    });

    try {
      await subject(filesystem).aliases();
      throw new Error('malformed included config unexpectedly resolved');
    } catch (error) {
      expect(error).toBeInstanceOf(SshConfigDiscoveryError);
      expect(error).toMatchObject({code: 'syntax', path: malformed});
      expect((error as Error).cause).toBeInstanceOf(SshConfigSyntaxError);
    }
  });

  test('bounds directory examination even when a broad pattern has no matches', async () => {
    const entries = Array.from({length: 16_385}, (_, index) => `entry-${index}`);
    const filesystem = new FakeSshConfigFilesystem(
      {[ROOT]: 'Include huge/*.conf'},
      {},
      {[`${HOME}/.ssh/huge`]: entries},
    );

    await expect(subject(filesystem).aliases()).rejects.toMatchObject({
      code: 'limit',
    });
  });

  test('bounds repeated evaluation independently of the unique parsed-file cache', async () => {
    const shared = `${HOME}/.ssh/shared.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [ROOT]: Array.from({length: 1025}, () => 'Include shared.conf').join('\n'),
      [shared]: 'Host shared',
    });

    await expect(subject(filesystem).aliases()).rejects.toThrow('1024-evaluation traversal limit');
    expect(filesystem.reads.filter(path => path === shared)).toHaveLength(1);
  });

  test('rejects expansions it cannot interpret instead of silently resolving another path', async () => {
    for (const pattern of [
      '~other/hosts.conf',
      ['$', '{HOME}/hosts.conf'].join(''),
      '%d/hosts.conf',
    ]) {
      const filesystem = new FakeSshConfigFilesystem({[ROOT]: `Include ${pattern}`});
      await expect(subject(filesystem).aliases()).rejects.toMatchObject({
        code: 'unsupported-expansion',
      });
    }
  });

  test('anchors relative Includes to home .ssh even when the root path is elsewhere', async () => {
    const root = '/custom/config';
    const included = `${HOME}/.ssh/relative.conf`;
    const filesystem = new FakeSshConfigFilesystem({
      [root]: 'Include relative.conf',
      [included]: 'Host home-relative',
    });
    const hosts = new Hosts({
      runs: idleRunWatch(),
      manager: new ConnectionManager({
        linkFor: () => {
          throw new Error('alias discovery must not connect');
        },
        appVersion: 'test',
        scheduleTimeout: new FakeClock().scheduleTimeout,
        log: () => {},
      }),
      settings: new SettingsStore({
        settingsPath: '/unused/hosts.json',
        recentPath: '/unused/recent.json',
        preferencesPath: '/unused/preferences.json',
        log: () => {},
      }),
      localCheckout: '/vibesys',
      sshConfigPath: root,
      sshConfigFilesystem: filesystem,
      sshHomePath: HOME,
      log: () => {},
      push: () => {},
      retire: () => {},
    });

    await expect(hosts.aliases()).resolves.toEqual(['home-relative']);
  });
});
