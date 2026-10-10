/**
 * `SshHost`'s remote commands under real login shells: the one test here that runs processes.
 *
 * ssh hands a remote command to the user's login shell, so the exact strings `SshHost` sends are run
 * here with `<shell> -c COMMAND`, the way sshd runs them, under every shell installed on this
 * machine (sh, dash, bash, zsh, tcsh, csh, fish; an absent one is skipped and says so). On the far
 * side, stub `vibesys` and `python` executables record their working directory and argv, so the
 * test checks that data survives each shell byte for byte, whatever it contains. The master
 * connection is not modelled: `-O` and master runs succeed, and only channels reach a shell.
 *
 * Processes run with `spawnSync`, so nothing here waits on time; the timeout only guards a hang.
 */
import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {createServer, type Server} from 'node:net';
import {delimiter, join} from 'node:path';
import type {Duplex} from 'node:stream';
import {HostError} from './host.js';
import type {SpawnedProcess} from './process.js';
import {SshHost, type SshRunner} from './ssh-host.js';
import {finishedProcess} from './testing/fake-process.js';

const SHELLS = ['sh', 'dash', 'bash', 'zsh', 'tcsh', 'csh', 'fish'] as const;

/** `name`'s path on this machine's PATH, or null. */
function which(name: string): string | null {
  for (const directory of (process.env['PATH'] ?? '').split(delimiter)) {
    const candidate = join(directory, name);
    if (directory !== '' && existsSync(candidate)) return candidate;
  }
  return null;
}

/** Records argv (after `$PWD`) NUL-separated into `$STUB_OUT`, then prints `{}`. */
const STUB = `#!/bin/sh
printf '%s\\0' "$PWD" "$@" > "$STUB_OUT"
echo '{}'
`;

let root = '';
let home = '';
let bin = '';
let out = '';
const servers: Server[] = [];

beforeAll(() => {
  // Short: Unix socket paths are capped near 104 bytes.
  root = realpathSync(mkdtempSync('/tmp/vss-'));
  home = join(root, 'h');
  bin = join(root, 'bin');
  out = join(root, 'out');
  mkdirSync(home);
  mkdirSync(bin);
  mkdirSync(join(home, 'bin'));
  for (const path of [join(bin, 'vibesys'), join(bin, 'python'), join(home, 'bin', 'vibesys')]) {
    writeFileSync(path, STUB);
    chmodSync(path, 0o755);
  }
});

afterAll(async () => {
  await Promise.all(servers.map(server => new Promise(resolve => server.close(resolve))));
  rmSync(root, {recursive: true, force: true});
});

/** An `SshRunner` whose channels run their remote command with `<shell> -c`, as sshd does. */
class LoginShellSsh implements SshRunner {
  readonly commands: string[] = [];
  readonly #shell: string;

  constructor(shell: string) {
    this.#shell = shell;
  }

  spawn(args: readonly string[]): SpawnedProcess {
    if (args.includes('-O') || args.includes('ControlMaster=auto')) return finishedProcess(0);
    const command = args[args.indexOf('--') + 2] ?? '';
    this.commands.push(command);
    rmSync(out, {force: true});
    const result = spawnSync(this.#shell, ['-c', command], {
      cwd: home,
      env: {PATH: `${bin}:/usr/bin:/bin`, HOME: home, STUB_OUT: out},
      timeout: 60_000,
    });
    return finishedProcess(result.status, result.stdout.toString(), result.stderr.toString());
  }
}

/** What the last stub run recorded: its working directory and its argv. */
function recorded(): {readonly cwd: string; readonly argv: readonly string[]} {
  const [cwd = '', ...argv] = readFileSync(out, 'utf8').split('\0').slice(0, -1);
  return {cwd, argv};
}

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

/** Everything a shell might touch: quotes, `$()`, backticks, `!`, globs, `~`, `%`, unicode. */
const AWKWARD = [...'ab -\'"$`\\;|&*?~ \t()<>{}[]#!%=^é✓', '$(', '`id`', '!!', '\\\\', '~/'];

function awkward(random: () => number, length: number): string {
  return Array.from({length}, () => AWKWARD[Math.floor(random() * AWKWARD.length)]).join('');
}

/** A directory or socket name: awkward, but a single path component other than `.`, `..`, `-`. */
function awkwardName(random: () => number): string {
  return `n${awkward(random, 1 + Math.floor(random() * 6)).replaceAll('/', '_')}`;
}

/** Start a run with `argv` in a new awkwardly named directory, as `~/NAME` or absolute. */
async function expectStartedIn(
  host: SshHost,
  random: () => number,
  argv: readonly string[],
): Promise<void> {
  const directory = awkwardName(random);
  const tilde = random() < 0.5;
  mkdirSync(join(tilde ? home : root, directory), {recursive: true});
  const cwd = tilde ? `~/${directory}` : join(root, directory);
  await host.startServer(argv, {cwd}).catch((error: unknown) => {
    // The stub prints `{}`, not a record; only its argv and directory matter here.
    if (!(error instanceof HostError && error.kind === 'malformed')) throw error;
  });
  expect(recorded()).toEqual({
    cwd: tilde ? join(home, directory) : cwd,
    argv: ['--detach', ...argv],
  });
}

function drain(stream: Duplex): Promise<void> {
  return new Promise((resolve, reject) => {
    stream.on('data', () => {});
    stream.once('end', resolve);
    stream.once('error', reject);
  });
}

async function listen(path: string): Promise<void> {
  const server = createServer(socket => socket.destroy());
  servers.push(server);
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(path, resolve);
  });
}

for (const name of SHELLS) {
  const shell = which(name);
  const label = shell === null ? `${name} (skipped: not installed)` : `under ${name}`;
  describe.skipIf(shell === null)(`SshHost remote commands ${label}`, () => {
    const make = (options: {vibesysCommand?: string; pythonCommand?: string} = {}) => {
      const ssh = new LoginShellSsh(shell ?? '');
      const host = new SshHost({
        alias: 'node-1',
        vibesysCommand: options.vibesysCommand ?? 'vibesys',
        ...(options.pythonCommand === undefined ? {} : {pythonCommand: options.pythonCommand}),
        controlPath: '/tmp/vsd/%C',
        runner: ssh,
      });
      return {ssh, host};
    };

    test('argv and the working directory reach vibesys byte for byte', async () => {
      const random = generator(20_261_010 + name.length);
      const {host} = make();
      for (let round = 0; round < 25; round += 1) {
        const argv = Array.from({length: Math.floor(random() * 4)}, () =>
          awkward(random, Math.floor(random() * 8)),
        );
        if (random() < 0.3) argv.unshift('-leading');
        await host.invoke(argv);
        expect(recorded()).toEqual({cwd: home, argv});

        await expectStartedIn(host, random, argv);
      }
      await host.close();
    });

    test('a command under ~/ is found and run', async () => {
      const {host} = make({vibesysCommand: '~/bin/vibesys'});
      await host.invoke(['instances', 'list', '--json']);
      expect(recorded().argv).toEqual(['instances', 'list', '--json']);
      await host.close();
    });

    test('the bridge gets the socket path byte for byte, and a missing socket is unreachable', async () => {
      const random = generator(7 + name.length);
      mkdirSync(join(root, `s-${name}`), {recursive: true});
      const {host} = make({pythonCommand: join(bin, 'python')});
      for (let round = 0; round < 8; round += 1) {
        const socketPath = join(root, `s-${name}`, `${round}${awkwardName(random)}`);
        await listen(socketPath);
        await drain(await host.dial({socketPath}));
        expect(recorded().argv).toEqual(['-m', 'entrypoints.stdio_bridge', '--socket', socketPath]);
      }
      await expect(
        host.dial({socketPath: join(root, 'missing $(id) `id` !!')}),
      ).rejects.toMatchObject({kind: 'unreachable'});
      await host.close();
    });

    test('a #!/bin/sh vibesys names no Python, so the user is asked for one', async () => {
      const {host} = make();
      await expect(host.dial({socketPath: join(root, 'any')})).rejects.toThrow(
        "set the host's Python command",
      );
      await host.close();
    });
  });
}

describe('SshHost data that cannot cross a command line', () => {
  test('a line break in any datum is refused before ssh runs', async () => {
    const ssh = new LoginShellSsh('/bin/sh');
    const host = new SshHost({
      alias: 'node-1',
      vibesysCommand: 'vibesys',
      controlPath: '/tmp/vsd/%C',
      runner: ssh,
    });
    await host.invoke(['ok']);
    const before = ssh.commands.length;
    for (const argv of [['a\nb'], ['ok', '\r'], ['\n']]) {
      await expect(host.invoke(argv)).rejects.toMatchObject({kind: 'failed'});
    }
    expect(ssh.commands.length).toBe(before);
    await host.close();
  });
});
