import {describe, expect, test} from 'bun:test';
import type {Duplex} from 'node:stream';
import {HostError, type HostErrorKind} from './host.js';
import {LocalHost} from './local-host.js';
import {PROBED_DIRECTORIES, SshHost} from './ssh-host.js';
import type {CommandResult} from './testing/fake-host.js';
import {fakeLocalSystem} from './testing/fake-local-system.js';
import {FAKE_VIBESYS_PATH, FAKE_VIBESYS_PYTHON, FakeSsh} from './testing/fake-ssh.js';
import {FakeVibesysNode} from './testing/fake-vibesys-node.js';

function world(
  options: {vibesysCommand?: string; command?: (argv: readonly string[]) => CommandResult} = {},
) {
  const node = new FakeVibesysNode({
    server: () => connection => connection.pipe(connection),
    command: options.command ?? (() => ({code: 0, stdout: '{}', stderr: ''})),
  });
  const ssh = new FakeSsh(node);
  const host = new SshHost({
    alias: 'node-1',
    vibesysCommand: options.vibesysCommand ?? 'vibesys',
    controlPath: '/tmp/vsd/%C',
    askpass: '/app/askpass',
    runner: ssh,
  });
  return {node, ssh, host};
}

async function kindOf(promise: Promise<unknown>): Promise<HostErrorKind> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof HostError) return error.kind;
    throw error;
  }
  throw new Error('expected a HostError');
}

function streamError(stream: Duplex): Promise<HostErrorKind | 'none'> {
  return new Promise(resolve => {
    stream.once('error', error => resolve(error instanceof HostError ? error.kind : 'none'));
    stream.once('close', () => resolve('none'));
  });
}

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

const AWKWARD = [...'ab -\'"$`\\;|&*?~ \t()<>{}#!é'];

function awkwardWord(random: () => number): string {
  const length = Math.floor(random() * 8);
  return Array.from({length}, () => AWKWARD[Math.floor(random() * AWKWARD.length)]).join('');
}

describe('SshHost', () => {
  test('only the master authenticates: every channel is BatchMode and carries no askpass', async () => {
    const {ssh, host} = world();
    const server = await host.startServer(['--project', '/p']);
    const stream = await host.dial(server.endpoint);
    stream.destroy();
    await host.invoke(['instances', 'list', '--json']);
    const channels = ssh.calls.filter(call => call.args.includes('ControlMaster=no'));
    expect(channels.length).toBeGreaterThan(0);
    for (const call of channels) {
      expect(call.args).toContain('BatchMode=yes');
      expect(call.env['SSH_ASKPASS']).toBeUndefined();
    }
    await host.close();
  });

  test('a password prompt happens once, through the askpass helper, on an explicit ensureLink', async () => {
    const {ssh, host} = world();
    ssh.keyLogin = false;
    expect(await kindOf(host.invoke(['x']))).toBe('auth');
    expect(ssh.prompts).toBe(0);
    await host.ensureLink();
    expect(ssh.prompts).toBe(1);
    const master = ssh.calls.find(call => call.env['SSH_ASKPASS'] !== undefined);
    expect(master?.env).toEqual({SSH_ASKPASS: '/app/askpass', SSH_ASKPASS_REQUIRE: 'force'});
    expect(master?.args).toContain('ServerAliveInterval=15');
    await host.invoke(['x']);
    await host.dial((await host.startServer([])).endpoint);
    expect(ssh.prompts).toBe(1);
    await host.close();
  });

  test('refused credentials are auth and an unreachable network is link', async () => {
    const refused = world();
    refused.ssh.keyLogin = false;
    refused.ssh.acceptsCredentials = false;
    expect(await kindOf(refused.host.ensureLink())).toBe('auth');
    const offline = world();
    offline.ssh.reachable = false;
    expect(await kindOf(offline.host.ensureLink())).toBe('link');
    expect(await kindOf(offline.host.invoke(['x']))).toBe('link');
  });

  test('a link that drops mid-stream ends the stream with link, and the next dial restores it', async () => {
    const {ssh, host} = world();
    const server = await host.startServer([]);
    const stream = await host.dial(server.endpoint);
    stream.resume();
    const ended = streamError(stream);
    ssh.dropLink();
    expect(await ended).toBe('link');
    const again = await host.dial(server.endpoint);
    expect(ssh.masterUp).toBe(true);
    again.destroy();
    await host.close();
  });

  test('a dial to a run that is gone is unreachable, and the stream of a stopped run says so', async () => {
    const {node, host} = world();
    const server = await host.startServer([]);
    const stream = await host.dial(server.endpoint);
    stream.resume();
    const ended = streamError(stream);
    node.stopAll();
    expect(await ended).toBe('none');
    expect(await kindOf(host.dial(server.endpoint))).toBe('unreachable');
    await host.close();
  });

  test('a missing vibesys command names every place tried', async () => {
    const {ssh, host} = world();
    ssh.installed = false;
    try {
      await host.invoke(['instances', 'list', '--json']);
      throw new Error('expected a failure');
    } catch (error) {
      expect((error as HostError).kind).toBe('failed');
      const message = (error as Error).message;
      expect(message).toContain('"vibesys"');
      for (const directory of PROBED_DIRECTORIES) expect(message).toContain(directory);
    }
  });

  test('the bridge runs on the Python of the resolved command', async () => {
    const installed = world();
    await installed.host.dial((await installed.host.startServer([])).endpoint);
    expect(installed.ssh.ran).toContain(FAKE_VIBESYS_PYTHON);
    expect(installed.ssh.ran[0]).toBe(`'${FAKE_VIBESYS_PATH}'`);
    await installed.host.close();

    const checkout = world({vibesysCommand: 'uv run --project ~/src/vibesys vibesys'});
    await checkout.host.dial((await checkout.host.startServer([])).endpoint);
    expect(checkout.ssh.ran).toContain(`'${FAKE_VIBESYS_PATH}' run --project ~/src/vibesys python`);
    await checkout.host.close();
  });

  test('argv reaches the remote vibesys word for word, whatever it contains', async () => {
    const random = generator(20_261_010);
    const seen: (readonly string[])[] = [];
    const {host} = world({
      command: argv => {
        seen.push(argv);
        return {code: 0, stdout: '{}', stderr: ''};
      },
    });
    for (let round = 0; round < 200; round += 1) {
      const argv = Array.from({length: 1 + Math.floor(random() * 4)}, () => awkwardWord(random));
      await host.invoke(argv);
      expect(seen.at(-1)).toEqual(argv);
    }
    await host.close();
  });
});

describe('detached starts', () => {
  test('a started server runs in the project directory on either host', async () => {
    const remote = world();
    await remote.host.startServer(['--project', '/srv/p'], {cwd: '/srv/p'});
    expect(remote.node.startDirectories).toEqual(['/srv/p']);
    await remote.host.close();

    const node = new FakeVibesysNode({
      server: () => () => {},
      command: () => ({code: 0, stdout: '{}', stderr: ''}),
    });
    const local = new LocalHost({python: ['python3'], system: fakeLocalSystem(node)});
    const server = await local.startServer(['--project', '/Users/me/p'], {cwd: '/Users/me/p'});
    expect(node.startDirectories).toEqual(['/Users/me/p']);
    expect(server.record.kind).toBe('compatible');
    await local.close();
  });
});
