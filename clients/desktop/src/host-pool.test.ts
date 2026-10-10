import {describe, expect, test} from 'bun:test';
import {mkdtempSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {HostPool} from './host-pool.js';

function pool(sshConfig: string, settings: string | null) {
  const directory = mkdtempSync(join(tmpdir(), 'vsd-pool-'));
  writeFileSync(join(directory, 'config'), sshConfig);
  if (settings !== null) writeFileSync(join(directory, 'hosts.json'), settings);
  const logged: string[] = [];
  const hosts = new HostPool({
    sshConfigPath: join(directory, 'config'),
    settingsPath: join(directory, 'hosts.json'),
    controlPath: '/tmp/vsd-test/%C',
    askpass: '/app/askpass',
    localPython: ['python3'],
    log: message => logged.push(message),
  });
  return {hosts, logged, directory};
}

describe('HostPool', () => {
  test('offers this machine and the concrete ssh aliases, with each host command', async () => {
    const {hosts} = pool(
      'Host gpu-box\nHost *.lab\nHost cluster\n',
      JSON.stringify({version: 1, hosts: {cluster: {vibesysCommand: 'uv run vibesys'}}}),
    );
    expect(await hosts.offered()).toEqual([
      {key: 'local', label: 'This Mac', vibesysCommand: null, pythonCommand: null},
      {key: 'ssh:gpu-box', label: 'gpu-box', vibesysCommand: 'vibesys', pythonCommand: ''},
      {key: 'ssh:cluster', label: 'cluster', vibesysCommand: 'uv run vibesys', pythonCommand: ''},
    ]);
  });

  test('resolves only hosts it offered', async () => {
    const {hosts} = pool('Host gpu-box\n', null);
    expect(await hosts.resolve('local')).toEqual({kind: 'local'});
    expect(await hosts.resolve('ssh:gpu-box')).toEqual({kind: 'ssh', alias: 'gpu-box'});
    for (const key of ['ssh:other', 'ssh:-oProxyCommand=x', 'gpu-box', 7, null]) {
      expect(await hosts.resolve(key)).toBeNull();
    }
  });

  test('saved commands are validated, persisted, and offered next time', async () => {
    const {hosts} = pool('Host gpu-box\n', null);
    await expect(hosts.saveCommands('gpu-box', 'x\ny', '')).rejects.toThrow('must be one line');
    await expect(hosts.saveCommands('gpu-box', 'vibesys', '-c x')).rejects.toThrow(
      'must start with a command',
    );
    await hosts.saveCommands(
      'gpu-box',
      ' uv  run --project ~/v vibesys ',
      ' /srv/venv/bin/python ',
    );
    expect((await hosts.offered())[1]).toMatchObject({
      vibesysCommand: 'uv run --project ~/v vibesys',
      pythonCommand: '/srv/venv/bin/python',
    });
    await hosts.saveCommands('gpu-box', 'vibesys', '  ');
    expect((await hosts.offered())[1]?.pythonCommand).toBe('');
  });

  test('an invalid settings file is reported and ignored, not fatal', async () => {
    const {hosts, logged} = pool('Host gpu-box\n', '{"version": 1, "hosts": {"gpu-box": {}}}');
    expect((await hosts.offered())[1]?.vibesysCommand).toBe('vibesys');
    expect(logged.join('\n')).toContain('vibesysCommand must be a string');
  });
});
