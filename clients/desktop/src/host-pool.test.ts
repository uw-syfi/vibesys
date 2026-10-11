import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {HostError} from './host.js';
import {type CheckedHost, HostPool} from './host-pool.js';
import type {HostId} from './host-settings.js';
import {SettingsStore} from './settings-store.js';
import {FakeHost} from './testing/fake-host.js';
import {fakeRecord} from './testing/fake-record.js';

interface Built {
  readonly id: HostId;
  readonly checkout: string;
}

function pool(
  sshConfig: string,
  settings: string | null,
  options: {
    readonly broken?: ReadonlySet<string>;
    readonly roots?: readonly string[];
    readonly rootsError?: HostError;
    /** What `vibesys tasks` prints on stderr when it fails; it succeeds when absent. */
    readonly tasksStderr?: string;
  } = {},
) {
  const directory = mkdtempSync(join(tmpdir(), 'vsd-pool-'));
  writeFileSync(join(directory, 'config'), sshConfig);
  if (settings !== null) writeFileSync(join(directory, 'hosts.json'), settings);
  const logged: string[] = [];
  const built: Built[] = [];
  const hosts = new HostPool({
    sshConfigPath: join(directory, 'config'),
    settings: new SettingsStore({
      settingsPath: join(directory, 'hosts.json'),
      recentPath: join(directory, 'recent.json'),
      log: message => logged.push(message),
    }),
    localCheckout: '/Users/me/src/vibesys',
    factory: async (id, checkout) => {
      built.push({id, checkout});
      const host = new FakeHost({
        command: argv =>
          argv[0] === 'tasks' && options.tasksStderr !== undefined
            ? {code: 1, stdout: '', stderr: options.tasksStderr}
            : argv[0] === 'tasks'
              ? {
                  code: 0,
                  stdout: JSON.stringify({
                    version: 1,
                    project_root: argv[1],
                    tasks: [{name: 'mpmc'}, {name: 'spsc'}],
                  }),
                  stderr: '',
                }
              : {
                  code: 0,
                  stdout: JSON.stringify({
                    version: 1,
                    instances: [
                      {...fakeRecord('0123456789ab', '/run/s', 1), vibesys_root: checkout},
                    ],
                  }),
                  stderr: '',
                },
      });
      return Object.assign(host, {
        verifyCheckout: async () => {
          if (options.broken?.has(checkout)) {
            throw new HostError('failed', `${checkout} is not a directory.`);
          }
          return checkout.replace('~', '/home/me');
        },
        registryRoots: async () => {
          if (options.rootsError !== undefined) throw options.rootsError;
          return [...(options.roots ?? [])];
        },
      }) satisfies CheckedHost;
    },
    log: message => logged.push(message),
  });
  return {hosts, logged, built, directory};
}

const GPU: HostId = {kind: 'ssh', alias: 'gpu-box'};

describe('HostPool', () => {
  test('lists the concrete ssh aliases', async () => {
    const {hosts} = pool('Host gpu-box\nHost *.lab\nHost cluster\n', null);
    expect(await hosts.aliases()).toEqual(['gpu-box', 'cluster']);
  });

  test('This Mac runs from the app checkout until the user sets one; other hosts need one', async () => {
    const {hosts, built} = pool('Host gpu-box\n', null);
    expect(await hosts.checkout({kind: 'local'})).toBe('/Users/me/src/vibesys');
    expect(await hosts.checkout(GPU)).toBeNull();
    await expect(hosts.host(GPU)).rejects.toThrow('Set the VibeSys checkout for gpu-box first.');
    await hosts.host({kind: 'local'});
    expect(built).toEqual([{id: {kind: 'local'}, checkout: '/Users/me/src/vibesys'}]);
  });

  test('a checkout is checked before it is saved, and the next use runs from it', async () => {
    const {hosts, directory, built} = pool('Host gpu-box\n', null, {
      broken: new Set(['/nowhere']),
    });
    await expect(hosts.setCheckout(GPU, 'relative/path')).rejects.toThrow(
      'the VibeSys checkout on gpu-box must be an absolute path',
    );
    await expect(hosts.setCheckout(GPU, '/nowhere')).rejects.toThrow(
      '/nowhere is not a directory.',
    );
    expect(await hosts.checkout(GPU)).toBeNull();

    expect(await hosts.setCheckout(GPU, ' ~/src/vibesys/ ')).toBe('/home/me/src/vibesys');
    expect(JSON.parse(readFileSync(join(directory, 'hosts.json'), 'utf8'))).toEqual({
      version: 2,
      hosts: {'ssh:gpu-box': {checkout: '~/src/vibesys'}},
    });
    await hosts.host(GPU);
    expect(built.at(-1)).toEqual({id: GPU, checkout: '~/src/vibesys'});
  });

  test('suggests a checkout from the live records of a host without one', async () => {
    const {hosts} = pool('Host gpu-box\n', null, {roots: ['/home/me/src/vibesys']});
    expect(await hosts.suggestedCheckout(GPU)).toBe('/home/me/src/vibesys');
  });

  test('regression: a host that cannot be reached or refuses the user says so instead of asking for a checkout', async () => {
    for (const kind of ['link', 'auth'] as const) {
      const error = new HostError(kind, `ssh: ${kind}`);
      const {hosts} = pool('Host gpu-box\n', null, {rootsError: error});
      await expect(hosts.suggestedCheckout(GPU)).rejects.toBe(error);
    }
    const {hosts} = pool('Host gpu-box\n', null, {
      rootsError: new HostError('failed', 'no registry'),
    });
    expect(await hosts.suggestedCheckout(GPU)).toBeNull();
  });

  test("lists a project's tasks and validates the project path first", async () => {
    const {hosts} = pool('', null);
    expect(await hosts.tasks({kind: 'local'}, '/tmp/queue-rs')).toEqual(['mpmc', 'spsc']);
    await expect(hosts.tasks({kind: 'local'}, '-x')).rejects.toThrow('cannot start with "-"');
  });

  test('regression: a failed task listing reads as plain language, not a command line', async () => {
    const {hosts, logged} = pool('', null, {
      tasksStderr: 'vibesys tasks: Project root does not exist: /x',
    });
    const listing = hosts.tasks({kind: 'local'}, '/x');
    await expect(listing).rejects.toThrow(
      'Could not read the tasks of /x: Project root does not exist: /x',
    );
    await listing.catch((error: Error) => expect(error.message).not.toContain('--json'));
    expect(logged.join('\n')).toContain('exited with 1');
  });

  test('remembers attached runs, most recent first', async () => {
    const {hosts} = pool('', null);
    const run = {
      host: 'local',
      project: '/tmp/queue-rs',
      task: 'spsc',
      instanceId: '0123456789ab',
      runId: null,
      attachedAt: 1,
    };
    await hosts.remember(run);
    await hosts.remember({...run, instanceId: 'ba9876543210', attachedAt: 2});
    expect((await hosts.recent()).runs.map(entry => entry.instanceId)).toEqual([
      'ba9876543210',
      '0123456789ab',
    ]);
  });

  test('regression: concurrent remembers and reads keep every run and never see a partial file', async () => {
    const {hosts, logged} = pool('', null);
    const ids = ['0123456789ab', 'ba9876543210', 'aaaaaaaaaaaa', 'bbbbbbbbbbbb', 'cccccccccccc'];
    const reads: Promise<unknown>[] = [];
    const writes = ids.map((instanceId, index) => {
      reads.push(hosts.recent());
      return hosts.remember({
        host: 'local',
        project: '/tmp/queue-rs',
        task: null,
        instanceId,
        runId: null,
        attachedAt: index,
      });
    });
    await Promise.all([...writes, ...reads]);
    const remembered = (await hosts.recent()).runs.map(entry => entry.instanceId);
    expect([...remembered].sort()).toEqual([...ids].sort());
    expect(logged.filter(line => line.startsWith('ignoring'))).toEqual([]);
  });

  test('an invalid settings file is reported and ignored, not fatal', async () => {
    const {hosts, logged} = pool(
      'Host gpu-box\n',
      '{"version": 1, "hosts": {"gpu-box": {"vibesysCommand": "vibesys"}}}',
    );
    expect(await hosts.checkout(GPU)).toBeNull();
    expect(await hosts.settingsCheck()).toContain('hosts.json is version 1');
    expect(logged.join('\n')).toContain('Remove the file');
  });
});
