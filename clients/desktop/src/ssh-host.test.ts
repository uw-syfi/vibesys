import {describe, expect, test} from 'bun:test';
import type {Duplex} from 'node:stream';
import {HostError, type HostErrorKind} from './host.js';
import {LocalHost} from './local-host.js';
import {checkoutProblem, PROBED_DIRECTORIES, SshHost, shellPath} from './ssh-host.js';
import type {CommandResult} from './testing/fake-host.js';
import {fakeLocalSystem} from './testing/fake-local-system.js';
import {fakeRecord} from './testing/fake-record.js';
import {FAKE_CHECKOUT_ROOT, FAKE_UV_PATH, FakeSsh} from './testing/fake-ssh.js';
import {FakeVibesysNode} from './testing/fake-vibesys-node.js';

function world(
  options: {checkout?: string; command?: (argv: readonly string[]) => CommandResult} = {},
) {
  const node = new FakeVibesysNode({
    server: () => connection => connection.pipe(connection),
    command: options.command ?? (() => ({code: 0, stdout: '{}', stderr: ''})),
  });
  const ssh = new FakeSsh(node);
  const host = new SshHost({
    alias: 'node-1',
    checkout: options.checkout ?? '/home/user/src/vibesys',
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

  test('a host key not yet trusted needs sign-in, which asks through askpass and never accepts silently', async () => {
    const {ssh, host} = world();
    ssh.hostKeyKnown = false;
    expect(await kindOf(host.invoke(['x']))).toBe('auth');
    expect(ssh.prompts).toBe(0);
    expect(ssh.hostKeyKnown).toBe(false);
    ssh.acceptsHostKey = false;
    expect(await kindOf(host.ensureLink())).toBe('auth');
    expect(ssh.hostKeyKnown).toBe(false);
    ssh.acceptsHostKey = true;
    await host.ensureLink();
    expect(ssh.hostKeyKnown).toBe(true);
    await host.invoke(['x']);
    await host.close();
  });

  test('the master persists for a bounded time, so one a crashed app left ends on its own', async () => {
    const {ssh, host} = world();
    await host.ensureLink();
    const master = ssh.calls.find(call => call.args.includes('ControlMaster=auto'));
    expect(master?.args).toContain('ControlPersist=30m');
    await host.close();
  });

  test('every remote command is one fixed single-quoted script and hex data, whatever the data', async () => {
    const random = generator(4_242);
    const {ssh, host} = world();
    for (let round = 0; round < 50; round += 1) {
      const argv = Array.from({length: Math.floor(random() * 4)}, () => awkwardWord(random));
      await host.invoke(argv);
      await host.startServer(argv, {cwd: `~/${awkwardWord(random)}`}).catch(() => {});
    }
    const commands = ssh.calls
      .filter(call => call.args.includes('ControlMaster=no'))
      .map(call => call.args.at(-1) ?? '');
    expect(commands.length).toBeGreaterThan(50);
    for (const command of commands) {
      // No quote, `!`, or line break inside the script; a backslash only before a digit (literal
      // in single quotes under sh, bash, zsh, csh, tcsh, and fish alike).
      expect(command).toMatch(/^sh -c '[^'!\n\r]*' vibesys-[a-z]+( x(?:[0-9a-f]{2})*)*$/);
      expect(command).not.toMatch(/\\[^0-9]/);
    }
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

  test("a bridge that ends abnormally is reported with the bridge's own outcome and detail", async () => {
    const {ssh, host} = world();
    const server = await host.startServer([]);
    const cases = [
      {status: 5, outcome: 'connect_denied', detail: 'Permission denied', kind: 'failed'},
      {status: 6, outcome: 'connect_failed', detail: 'timed out', kind: 'link'},
      {status: 9, outcome: 'server_failed', detail: 'reset', kind: 'link'},
      {status: 4, outcome: 'run_gone', detail: 'no socket', kind: 'unreachable'},
    ] as const;
    for (const {kind, ...refusal} of cases) {
      ssh.bridgeRefusal = refusal;
      try {
        await host.dial(server.endpoint);
        throw new Error('expected a failure');
      } catch (error) {
        expect((error as HostError).kind).toBe(kind);
        expect((error as Error).message).toContain(`${refusal.outcome}: ${refusal.detail}`);
      }
    }
    await host.close();
  });

  test('a bridge that ends right after it is ready still ends its stream, with its report', async () => {
    const {ssh, host} = world();
    const server = await host.startServer([]);
    const cases = [
      {status: 0, outcome: 'client_closed', detail: '', kind: 'none'},
      {status: 3, outcome: 'server_closed', detail: '', kind: 'none'},
      {status: 5, outcome: 'connect_denied', detail: 'Permission denied', kind: 'failed'},
      {status: 6, outcome: 'connect_failed', detail: 'timed out', kind: 'link'},
      {status: 7, outcome: 'server_stalled', detail: 'no progress', kind: 'link'},
    ] as const;
    for (const {kind, ...refusal} of cases) {
      ssh.bridgeRefusal = {...refusal, afterReady: true};
      const stream = await host.dial(server.endpoint);
      const ended = streamError(stream);
      stream.resume();
      expect(await ended).toBe(kind);
    }
    await host.close();
  });

  test('a missing uv names every place tried', async () => {
    const {ssh, host} = world();
    ssh.installed = false;
    try {
      await host.invoke(['instances', 'list', '--json']);
      throw new Error('expected a failure');
    } catch (error) {
      expect((error as HostError).kind).toBe('failed');
      const message = (error as Error).message;
      expect(message).toContain('uv was not found on node-1');
      expect(message).toContain("login shell's PATH");
      for (const directory of PROBED_DIRECTORIES) expect(message).toContain(`${directory}/uv`);
    }
  });

  test('commands and the bridge run from the checkout through the resolved uv', async () => {
    const absolute = world();
    await absolute.host.dial((await absolute.host.startServer([])).endpoint);
    expect(absolute.ssh.ran).toEqual([
      `'${FAKE_UV_PATH}' run --project '/home/user/src/vibesys' vibesys`,
      `'${FAKE_UV_PATH}' run --project '/home/user/src/vibesys' python`,
    ]);
    await absolute.host.close();

    const home = world({checkout: '~/src/vibesys'});
    await home.host.dial((await home.host.startServer([])).endpoint);
    expect(home.ssh.ran).toContain(`'${FAKE_UV_PATH}' run --project "$HOME"/'src/vibesys' python`);
    await home.host.close();
  });

  test('a checkout path is one sh word whatever it contains', () => {
    expect(shellPath("/srv/it's here")).toBe(`'/srv/it'\\''s here'`);
    expect(shellPath('~')).toBe('"$HOME"');
    expect(shellPath('~/a b')).toBe(`"$HOME"/'a b'`);
  });

  test('verifyCheckout resolves the physical checkout or names what is missing', async () => {
    const {ssh, host} = world({checkout: '~/src/vibesys'});
    expect(await host.verifyCheckout()).toBe(FAKE_CHECKOUT_ROOT);
    for (const verdict of ['nodir', 'nopyproject', 'notvibesys'] as const) {
      ssh.checkout = verdict;
      await expect(host.verifyCheckout()).rejects.toThrow(
        checkoutProblem(verdict, '~/src/vibesys on node-1'),
      );
    }
    ssh.checkout = 'ok';
    ssh.installed = false;
    await expect(host.verifyCheckout()).rejects.toThrow('uv was not found on node-1');
    await host.close();
  });

  test('checkout problems say what is wrong in words', () => {
    expect(checkoutProblem('nodir', '/x on h')).toBe('/x on h is not a directory.');
    expect(checkoutProblem('nopyproject', '/x on h')).toContain('has no pyproject.toml');
    expect(checkoutProblem('notvibesys', '/x on h')).toContain('does not declare the vibesys');
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

describe('detached launch and stop contracts', () => {
  const failure = (code: string, extra: object = {}) => ({
    version: 1,
    outcome: 'failed',
    code,
    stage: 'launch',
    message: `launch said ${code}`,
    exit_code: 2,
    log_path: null,
    live_instance: null,
    ...extra,
  });

  function launching(launchFailure: (args: readonly string[], node: FakeVibesysNode) => object) {
    const node = new FakeVibesysNode({
      server: () => () => {},
      command: () => ({code: 0, stdout: '{}', stderr: ''}),
      launchFailure: (args, self) =>
        args.includes('--resume') ? ({exit_code: 2, ...launchFailure(args, self)} as never) : null,
    });
    const ssh = new FakeSsh(node);
    const host = new SshHost({
      alias: 'node-1',
      checkout: '/home/user/src/vibesys',
      controlPath: '/tmp/vsd/%C',
      runner: ssh,
    });
    return {node, host};
  }

  test('resuming a run another server drives attaches to that server instead', async () => {
    const {node, host} = launching((_args, self) => {
      const [id, socketPath] = [...self.live.entries()][0] ?? ['', ''];
      return failure('run_already_live', {live_instance: fakeRecord(id, socketPath)});
    });
    const first = await host.startServer(['--project', '/p']);
    expect(first.alreadyLive).toBe(false);
    const again = await host.startServer(['--resume', 'run-1'], {cwd: '/p'});
    expect(again.alreadyLive).toBe(true);
    expect(again.endpoint).toEqual(first.endpoint);
    expect(node.live.size).toBe(1);
    (await host.dial(again.endpoint)).destroy();
    await host.close();
  });

  test('a launch that starts nothing is reported with its own code, message, and log', async () => {
    for (const [code, extra, expected] of [
      ['resume_not_found', {}, 'launch said resume_not_found (resume_not_found at launch).'],
      ['invalid_arguments', {}, 'invalid_arguments'],
      ['registry_unavailable', {exit_code: 1}, 'registry_unavailable'],
      ['server_start_failed', {log_path: '/run/x/server.log'}, 'Its log is /run/x/server.log.'],
    ] as const) {
      const {host} = launching(() => failure(code, extra));
      try {
        await host.startServer(['--resume', 'r'], {cwd: '/p'});
        throw new Error('expected a failure');
      } catch (error) {
        expect((error as HostError).kind).toBe('failed');
        expect((error as Error).message).toContain(expected);
      }
      await host.close();
    }
  });

  test('a stop reports the outcome the host observed', async () => {
    const {host} = launching(() => failure('x'));
    const server = await host.startServer([]);
    await server.stop();
    expect((await server.exited).stopOutcome).toBe('stopped');
    await host.close();
  });
});
