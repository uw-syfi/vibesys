import {describe, expect, test} from 'bun:test';
import {mkdtempSync, readFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {HostError} from './host.js';
import {type CheckedHost, HostPool} from './host-pool.js';
import type {HostId} from './host-settings.js';
import {FakeHost} from './testing/fake-host.js';
import {fakeRecord} from './testing/fake-record.js';

interface Built {
  readonly id: HostId;
  readonly checkout: string;
}

function pool(
  sshConfig: string,
  settings: string | null,
  options: {readonly broken?: ReadonlySet<string>; readonly roots?: readonly string[]} = {},
) {
  const directory = mkdtempSync(join(tmpdir(), 'vsd-pool-'));
  writeFileSync(join(directory, 'config'), sshConfig);
  if (settings !== null) writeFileSync(join(directory, 'hosts.json'), settings);
  const logged: string[] = [];
  const built: Built[] = [];
  const hosts = new HostPool({
    sshConfigPath: join(directory, 'config'),
    settingsPath: join(directory, 'hosts.json'),
    recentPath: join(directory, 'recent.json'),
    localCheckout: '/Users/me/src/vibesys',
    factory: async (id, checkout) => {
      built.push({id, checkout});
      const host = new FakeHost({
        command: argv =>
          argv[0] === 'tasks'
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
                  instances: [{...fakeRecord('0123456789ab', '/run/s', 1), vibesys_root: checkout}],
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
        registryRoots: async () => [...(options.roots ?? [])],
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

  test("lists a project's tasks and validates the project path first", async () => {
    const {hosts} = pool('', null);
    expect(await hosts.tasks({kind: 'local'}, '/tmp/queue-rs')).toEqual(['mpmc', 'spsc']);
    await expect(hosts.tasks({kind: 'local'}, '-x')).rejects.toThrow('cannot start with "-"');
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
