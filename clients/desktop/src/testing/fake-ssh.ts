/**
 * `FakeSsh`: an in-memory system `ssh` client and the machine behind it, so `SshHost` runs the Host
 * contract and the reconnection scenarios with no real ssh.
 *
 * It reads ssh's argv the way OpenSSH does for the options `SshHost` uses: `-O check|exit` talk to
 * the master; `ControlMaster=auto` establishes it (asking for a password through the askpass
 * environment); `ControlMaster=no` runs a channel over the master or, with the master down, over a
 * direct BatchMode connection, which fails with status 255 when the network or keys do. A
 * channel's remote command is `sh -c 'SCRIPT' ROLE xHEX...`; the role says which of `SshHost`'s
 * fixed scripts it is, and the machine is a `FakeVibesysNode`. The scripts themselves run under real
 * shells in `ssh-host.shells.test.ts`; here each role's effect is modelled directly.
 */
import type {Duplex} from 'node:stream';
import type {SpawnedProcess} from '../process.js';
import type {SshRunner} from '../ssh-host.js';
import {type FakeProcess, fakeProcess, finishedProcess} from './fake-process.js';
import type {FakeVibesysNode} from './fake-vibesys-node.js';

/** Where the fake machine has VibeSys installed. */
export const FAKE_VIBESYS_PATH = '/home/user/.local/bin/vibesys';
export const FAKE_VIBESYS_PYTHON = '/home/user/.local/share/uv/tools/vibesys/bin/python';

export interface FakeSshCall {
  readonly args: readonly string[];
  readonly env: Readonly<Record<string, string>>;
}

export class FakeSsh implements SshRunner {
  /** Whether the network reaches the host at all. */
  reachable = true;
  /** Whether the user's keys or agent log in without a prompt. */
  keyLogin = true;
  /** Whether the host accepts what the user types into the askpass dialog. */
  acceptsCredentials = true;
  /** Whether the host's key is in `known_hosts` already. */
  hostKeyKnown = true;
  /** Whether the user answers "yes" when asked to trust the host's key. */
  acceptsHostKey = true;
  /** Whether `vibesys` is installed where the probe finds it. */
  installed = true;
  /** The first line of the installed `vibesys`. */
  firstLine = `#!${FAKE_VIBESYS_PYTHON}`;
  /**
   * When set, every bridge exits at once with this outcome, as `stdio_bridge` reports it: before
   * the ready marker (the socket check failed) or, with `afterReady`, right after it.
   */
  bridgeRefusal: {
    readonly status: number;
    readonly outcome: string;
    readonly detail: string;
    readonly afterReady?: boolean;
  } | null = null;
  readonly calls: FakeSshCall[] = [];
  /** How many times a master connection could have prompted the user. */
  prompts = 0;
  /** The resolved commands and Pythons the channels ran, in order. */
  readonly ran: string[] = [];
  readonly #node: FakeVibesysNode;
  readonly #channels = new Set<FakeProcess>();
  #masterUp = false;

  constructor(node: FakeVibesysNode) {
    this.#node = node;
  }

  get masterUp(): boolean {
    return this.#masterUp;
  }

  /** The master connection dies (a dropped network, a slept laptop): every channel ends with 255. */
  dropLink(): void {
    this.#masterUp = false;
    for (const channel of [...this.#channels]) {
      channel.end(255, '', 'mux_client_read_packet: read header failed: Broken pipe\n');
    }
    this.#channels.clear();
  }

  /** How many channels are open. */
  get openChannels(): number {
    return this.#channels.size;
  }

  spawn(args: readonly string[], env: Readonly<Record<string, string>>): SpawnedProcess {
    this.calls.push({args, env});
    const separator = args.indexOf('--');
    if (separator < 0 || args[separator + 1] === undefined) {
      return finishedProcess(255, '', 'usage: ssh destination\n');
    }
    const options = args.slice(0, separator);
    const control = options[options.indexOf('-O') + 1];
    if (options.includes('-O')) {
      if (control === 'check') return finishedProcess(this.#masterUp ? 0 : 255);
      this.#masterUp = false;
      return finishedProcess(0);
    }
    if (options.includes('ControlMaster=auto')) return this.#establish(options, env);
    if (!options.includes('ControlMaster=no') || !options.includes('BatchMode=yes')) {
      throw new Error(`FakeSsh: a channel must never authenticate: ${args.join(' ')}`);
    }
    if (!this.#masterUp) {
      // OpenSSH falls back to a direct connection, which BatchMode limits to keys and the agent.
      const direct = this.#login(false, env);
      if (direct !== null) return direct;
    }
    return this.#remote(parseRemote(args[separator + 2] ?? ''));
  }

  /** Run one channel's remote command on the fake machine. */
  #remote({role, rest}: {readonly role: string; readonly rest: readonly string[]}): SpawnedProcess {
    if (role === 'vibesys-probe') {
      return finishedProcess(
        0,
        this.installed ? `found ${FAKE_VIBESYS_PATH}\nfirst ${this.firstLine}\n` : 'missing\n',
      );
    }
    if (role === 'vibesys-run') return this.#run(rest);
    if (role === 'vibesys-bridge') return this.#bridge(rest);
    throw new Error(`FakeSsh: unknown remote role ${role}`);
  }

  #establish(options: readonly string[], env: Readonly<Record<string, string>>): SpawnedProcess {
    const refused = this.#login(!options.includes('BatchMode=yes'), env);
    if (refused !== null) return refused;
    this.#masterUp = true;
    return finishedProcess(0);
  }

  /** Log in to the host; null on success, else ssh's failed process. */
  #login(prompts: boolean, env: Readonly<Record<string, string>>): SpawnedProcess | null {
    if (!this.reachable) {
      return finishedProcess(
        255,
        '',
        'ssh: connect to host node-1 port 22: Network is unreachable\n',
      );
    }
    if (prompts) this.prompts += 1;
    const answered =
      prompts && env['SSH_ASKPASS'] !== undefined && env['SSH_ASKPASS_REQUIRE'] === 'force';
    if (!this.hostKeyKnown) {
      // StrictHostKeyChecking=ask: BatchMode cannot ask; an interactive login asks through askpass.
      if (!answered || !this.acceptsHostKey) {
        return finishedProcess(
          255,
          '',
          'No ED25519 host key is known for node-1 and you have requested strict checking.\nHost key verification failed.\n',
        );
      }
      this.hostKeyKnown = true;
    }
    if (!this.keyLogin && (!answered || !this.acceptsCredentials)) {
      return finishedProcess(255, '', 'user@node-1: Permission denied (keyboard-interactive).\n');
    }
    return null;
  }

  #run([cwd, command, ...argv]: readonly string[]): SpawnedProcess {
    this.ran.push(command ?? '');
    const result = this.#node.run(argv, cwd === '' ? undefined : cwd);
    return this.#track(fakeProcess(), channel =>
      channel.end(result.code, result.stdout, result.stderr),
    );
  }

  #bridge([python, socketPath]: readonly string[]): SpawnedProcess {
    this.ran.push(python ?? '');
    const path = socketPath ?? '';
    const refusal = this.bridgeRefusal;
    if (refusal !== null) {
      const {status, outcome, detail} = refusal;
      return finishedProcess(
        status,
        refusal.afterReady === true ? 'R' : '',
        `${JSON.stringify({outcome, exit_status: status, detail})}\n`,
      );
    }
    if (!this.#node.network.isListening(path)) {
      return finishedProcess(
        4,
        '',
        '{"outcome": "run_gone", "exit_status": 4, "detail": "no socket"}\n',
      );
    }
    let connection: Duplex | null = null;
    const channel = fakeProcess(() => connection?.destroy());
    channel.stdout.write('R');
    void this.#node.network.connect(path).then(opened => {
      connection = opened;
      channel.input.on('data', (chunk: Buffer) => opened.write(chunk));
      channel.input.once('end', () => {
        opened.destroy();
        channel.end(0);
      });
      opened.on('data', (chunk: Buffer) => channel.stdout.write(chunk));
      opened.once('close', () =>
        channel.end(3, '', '{"outcome": "server_closed", "exit_status": 3, "detail": ""}\n'),
      );
    });
    return this.#track(channel, () => {});
  }

  #track(channel: FakeProcess, start: (channel: FakeProcess) => void): SpawnedProcess {
    this.#channels.add(channel);
    void channel.process.exit.then(() => this.#channels.delete(channel));
    start(channel);
    return channel.process;
  }
}

/**
 * Read `sh -c 'SCRIPT' ROLE xHEX...`, the only remote command shape `SshHost` sends: a script with no
 * single quote in it, a role, and hex-encoded data.
 */
function parseRemote(line: string): {readonly role: string; readonly rest: readonly string[]} {
  const prefix = "sh -c '";
  const close = line.indexOf("'", prefix.length);
  if (!line.startsWith(prefix) || close < 0) {
    throw new Error(`FakeSsh: unexpected remote command ${line}`);
  }
  const [role = '', ...data] = line.slice(close + 2).split(' ');
  const rest = data.map(word => {
    if (!/^x(?:[0-9a-f]{2})*$/.test(word)) throw new Error(`FakeSsh: undecodable datum ${word}`);
    return Buffer.from(word.slice(1), 'hex').toString('utf8');
  });
  return {role, rest};
}
