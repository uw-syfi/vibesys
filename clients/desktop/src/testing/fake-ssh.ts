/**
 * `FakeSsh`: an in-memory system `ssh` client and the machine behind it, so `SshHost` runs the Host
 * contract and the reconnection scenarios with no real ssh.
 *
 * It reads ssh's argv the way OpenSSH does for the options `SshHost` uses: `-O check|exit` talk to
 * the master; `ControlMaster=auto` establishes it (asking for a password through the askpass
 * environment); `ControlMaster=no` runs a channel, which fails with status 255 when the master is
 * down. A channel's remote command is `sh -c SCRIPT ROLE ARGS...`, single-quoted; the role says
 * which of `SshHost`'s fixed scripts it is, and the machine is a `FakeVibesysNode`.
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
  /** Whether `vibesys` is installed where the probe finds it. */
  installed = true;
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
      return finishedProcess(
        255,
        '',
        'Control socket connect(/tmp/cm): No such file or directory\nuser@host: Permission denied (publickey).\n',
      );
    }
    return this.#remote(parseWords(args[separator + 2] ?? ''));
  }

  /** Run one channel's remote command, `sh -c SCRIPT ROLE ARGS...`, on the fake machine. */
  #remote(words: readonly string[]): SpawnedProcess {
    const [sh, flag, , role, ...rest] = words;
    if (sh !== 'sh' || flag !== '-c') {
      throw new Error(`FakeSsh: unexpected remote command ${words.join(' ')}`);
    }
    if (role === 'vibesys-probe') {
      return finishedProcess(
        0,
        this.installed
          ? `found ${FAKE_VIBESYS_PATH}\nshebang ${FAKE_VIBESYS_PYTHON}\n`
          : 'missing\n',
      );
    }
    if (role === 'vibesys-run') return this.#run(rest);
    if (role === 'vibesys-bridge') return this.#bridge(rest);
    throw new Error(`FakeSsh: unknown remote role ${role}`);
  }

  #establish(options: readonly string[], env: Readonly<Record<string, string>>): SpawnedProcess {
    if (!this.reachable) {
      return finishedProcess(
        255,
        '',
        'ssh: connect to host node-1 port 22: Network is unreachable\n',
      );
    }
    const prompts = !options.includes('BatchMode=yes');
    if (prompts) this.prompts += 1;
    const answered =
      prompts && env['SSH_ASKPASS'] !== undefined && env['SSH_ASKPASS_REQUIRE'] === 'force';
    if (!this.keyLogin && (!answered || !this.acceptsCredentials)) {
      return finishedProcess(255, '', 'user@node-1: Permission denied (keyboard-interactive).\n');
    }
    this.#masterUp = true;
    return finishedProcess(0);
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

/** Split a POSIX command line made only of single-quoted words (as `shellQuote` writes them). */
function parseWords(line: string): string[] {
  const words: string[] = [];
  let word = '';
  let inWord = false;
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index] as string;
    if (quoted) {
      if (char === "'") quoted = false;
      else word += char;
    } else if (char === "'") {
      quoted = true;
      inWord = true;
    } else if (char === '\\') {
      index += 1;
      word += line[index] ?? '';
      inWord = true;
    } else if (char === ' ') {
      if (inWord) words.push(word);
      word = '';
      inWord = false;
    } else {
      word += char;
      inWord = true;
    }
  }
  if (inWord) words.push(word);
  return words;
}
