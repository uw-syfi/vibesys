/**
 * `SshHost`: the `Host` role on a machine reached with the system OpenSSH client.
 *
 * Using `ssh` itself (not a JavaScript SSH library) keeps the user's `~/.ssh/config`, ProxyJump,
 * agent, certificates, and Kerberos working unchanged. The app owns one master connection per host
 * (`ControlMaster`/`ControlPath`/`ControlPersist`), and only the master authenticates: it is the
 * one ssh started with an askpass helper, so a password or 2FA prompt appears once, as a dialog.
 * The master carries `ServerAliveInterval`, so a half-open link ends it instead of hanging, and
 * `ControlPersist=30m`, so a master an app crash left behind ends on its own. Every other ssh run is
 * a channel over that master with `BatchMode=yes`: it never prompts. When the master is gone, ssh
 * connects directly with keys and the agent only, and fails with its status 255 when that cannot
 * log in; this host reports that as `auth` (a refusal, or a host key not yet trusted) or `link`.
 *
 * - `invoke(argv)` runs `<vibesys command> argv` on the host.
 * - `startServer(args)` runs `<vibesys command> --detach args` and returns the record's socket.
 * - `dial(endpoint)` opens one channel per connection running the stdio bridge
 *   (`<python> -m entrypoints.stdio_bridge --socket PATH`), whose stdin and stdout are the stream.
 *
 * Remote commands must mean the same under any login shell (sh, bash, zsh, tcsh, fish), since ssh
 * hands them to that shell. Each is `sh -c '<fixed one-line script>' <role> <data>...`: the script
 * contains no single quote, `!`, newline, or backslash pair, so every one of those shells passes it
 * to `sh` unchanged, and each datum (paths, the configured command, argv) is hex-encoded (`x6869`),
 * which no shell alters; the script decodes them back into `"$@"`. Nothing from a page or a record
 * is ever spliced into script text. The configured vibesys command is the one thing `sh` evaluates,
 * because it is a command line the user typed in the app's settings (`uv run --project
 * ~/src/vibesys vibesys`). Non-interactive ssh sessions often lack `~/.local/bin` on PATH, so its
 * first word is probed there and in a few common places, and a failure names every place tried.
 */
import {spawn} from 'node:child_process';
import {Duplex} from 'node:stream';
import {DetachedHost, type HostAccess} from './detached-host.js';
import {HostError, type HostErrorKind} from './host.js';
import {type CommandOutput, finish, fromChild, type SpawnedProcess} from './process.js';

/** Runs the system `ssh` client with `args` and extra environment variables. */
export interface SshRunner {
  spawn(args: readonly string[], env: Readonly<Record<string, string>>): SpawnedProcess;
}

export interface SshHostOptions {
  /** The `Host` alias from `~/.ssh/config` (validated by `host-settings.ts`). */
  readonly alias: string;
  /** The command line that runs VibeSys on the host, e.g. `vibesys`. */
  readonly vibesysCommand: string;
  /** The command line that runs the Python VibeSys is installed in; derived when absent. */
  readonly pythonCommand?: string;
  /** The master connection's socket path on this machine; short (Unix sockets cap paths). */
  readonly controlPath: string;
  /** The askpass helper the master uses for password and 2FA prompts. */
  readonly askpass?: string;
  readonly runner?: SshRunner;
}

/** Directories probed for the vibesys command's first word when it is not on the host's PATH. */
export const PROBED_DIRECTORIES = [
  '$HOME/.local/bin',
  '$HOME/.cargo/bin',
  '/opt/homebrew/bin',
  '/usr/local/bin',
] as const;

/**
 * Decodes the hex-encoded arguments back into `"$@"`, and defines `d HEX` for the scripts' own
 * constants. `\134` (a backslash) is the one backslash in any script: a backslash before a digit is
 * literal inside single quotes in every login shell, fish included.
 */
// Lines with shell `${...}` expansions are template literals that escape them as `\${`.
const DECODE = [
  'b=$(printf "\\134");',
  `d() { h=\${1#x}; r=; while [ -n "$h" ]; do t=\${h#??}; p=\${h%"$t"}; v=$((0x$p));`,
  `r="$r\${b}0$((v / 64))$((v / 8 % 8))$((v % 8))"; h=$t; done; printf %b "$r"; };`,
  'n=$#; while [ "$n" -gt 0 ]; do set -- "$@" "$(d "$1")"; shift; n=$((n - 1)); done;',
].join(' ');

/** `"$@"` and `"$2"`, for the scripts that hand data to `eval`. */
const QUOTED_ALL = hex('"$@"');
const QUOTED_SECOND = hex('"$2"');

/**
 * Resolve the configured command's first word: on PATH, then in each probed directory. Prints
 * `found PATH` and `first LINE` (the resolved file's first line, its `#!` line for a script), or
 * `missing`.
 */
const PROBE_SCRIPT = [
  DECODE,
  'set -f; set -- $1; w=$1; eval "w=$w"; found=;',
  'case $w in */*) if [ -x "$w" ]; then found=$w; fi ;; *) found=$(command -v "$w" 2>/dev/null) || found= ;; esac;',
  `if [ -z "$found" ]; then case $w in */*) ;; *) for x in ${PROBED_DIRECTORIES.map(d => `"${d}"`).join(' ')}; do if [ -x "$x/$w" ]; then found=$x/$w; break; fi; done ;; esac; fi;`,
  'if [ -z "$found" ]; then echo missing; exit 0; fi;',
  'printf "found %s" "$found"; echo;',
  'l=$(dd if="$found" bs=256 count=1 2>/dev/null | head -n 1);',
  'printf "first %s" "$l"; echo',
].join(' ');

/** Run the resolved command (`$2`) with the remaining arguments, in directory `$1` when given. */
const RUN_SCRIPT = [
  DECODE,
  `case $1 in "") ;; "~/"*) cd -- "$HOME/\${1#??}" || exit 1 ;; *) cd -- "$1" || exit 1 ;; esac;`,
  `shift; c=$1; shift; e=$(d ${QUOTED_ALL}); eval "exec $c $e"`,
].join(' ');

/**
 * Open one bridge connection to socket `$2` with Python command `$1`. Prints one `R` once the
 * socket file exists, so `dial` can tell a missing run (status 4, as the bridge reports it) from a
 * connection that is up.
 */
const BRIDGE_SCRIPT = [
  DECODE,
  '[ -S "$2" ] || exit 4; printf R;',
  `e=$(d ${QUOTED_SECOND}); eval "exec $1 -m entrypoints.stdio_bridge --socket $e"`,
].join(' ');

/** The marker `BRIDGE_SCRIPT` prints before the bridge's own bytes. */
const BRIDGE_READY = 0x52;
/** ssh's own exit status for a connection or authentication failure. */
const SSH_FAILED = 255;

/**
 * The stdio bridge's exit statuses (`BridgeOutcome` in `src/server/stdio_bridge.py`) and the host
 * error each means for a stream; null is an ordinary end.
 */
const BRIDGE_EXITS: ReadonlyMap<number, HostErrorKind | null> = new Map([
  [0, null], // client_closed
  [3, null], // server_closed
  [4, 'unreachable'], // run_gone
  [5, 'failed'], // connect_denied: this user may not use the socket; retrying cannot help
  [6, 'link'], // connect_failed
  [7, 'link'], // server_stalled
  [8, 'link'], // client_stalled
  [9, 'link'], // server_failed
  [10, 'link'], // client_failed
  [SSH_FAILED, 'link'],
]);

/** Single-quote `word` for `sh` (never for a login shell: those see only `remoteCommand`). */
function shellQuote(word: string): string {
  return `'${word.replaceAll("'", `'\\''`)}'`;
}

/** `word` as a token every shell leaves alone: `x` and its UTF-8 bytes in hex. */
function hex(word: string): string {
  return `x${Buffer.from(word, 'utf8').toString('hex')}`;
}

/**
 * The remote command line that runs fixed `script` as `sh -c` with `role` and data `args`. Rejects
 * a datum with a newline or NUL: no command line this app runs needs one, and `sh` cannot carry it
 * through a command substitution intact.
 */
function remoteCommand(script: string, role: string, args: readonly string[]): string {
  for (const arg of args) {
    if (/[\n\r\0]/.test(arg)) {
      throw new HostError('failed', `an argument contains a line break: ${JSON.stringify(arg)}`);
    }
  }
  return ['sh', '-c', `'${script}'`, role, ...args.map(hex)].join(' ');
}

interface Resolved {
  /** The vibesys command with its first word replaced by the path found. */
  readonly vibesys: string;
  /** The Python the bridge runs on, or why it is unknown (commands still run without it). */
  readonly python: {readonly command: string} | {readonly problem: string};
}

export class SshHost extends DetachedHost {
  constructor(options: SshHostOptions) {
    super(new SshAccess(options));
  }
}

class SshAccess implements HostAccess {
  readonly #options: SshHostOptions;
  readonly #runner: SshRunner;
  #link: Promise<void> | null = null;
  #resolved: Promise<Resolved> | null = null;

  constructor(options: SshHostOptions) {
    this.#options = options;
    this.#runner = options.runner ?? nodeRunner;
  }

  /** Establish the master, prompting through the askpass helper when the host asks. */
  async ensureLink(): Promise<void> {
    try {
      await this.#ensure(false);
    } catch (error) {
      if (
        !(error instanceof HostError && error.kind === 'auth') ||
        this.#options.askpass === undefined
      ) {
        throw error;
      }
      await this.#ensure(true);
    }
  }

  /**
   * Establish the master if it is down; one attempt at a time. Only an interactive attempt (the
   * user's own retry, through `ensureLink`) may prompt: a channel restoring the master on its own
   * uses keys and the agent only, so a page's redial never pops up a password dialog.
   */
  #ensure(interactive: boolean): Promise<void> {
    this.#link ??= this.#establish(interactive).finally(() => {
      this.#link = null;
    });
    return this.#link;
  }

  async run(argv: readonly string[], cwd: string | undefined): Promise<CommandOutput> {
    await this.#ensure(false);
    const {vibesys} = await this.#resolve();
    const output = await finish(
      this.#channel(RUN_SCRIPT, 'vibesys-run', [cwd ?? '', vibesys, ...argv]),
    );
    if (output.code === SSH_FAILED) throw this.#sshError(output.stderr);
    return output;
  }

  async connect(socketPath: string): Promise<Duplex> {
    await this.#ensure(false);
    const {python} = await this.#resolve();
    if ('problem' in python) throw new HostError('failed', python.problem);
    const process = this.#channel(BRIDGE_SCRIPT, 'vibesys-bridge', [python.command, socketPath]);
    const stderr = new Tail();
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => stderr.push(chunk));
    const first = await firstChunk(process);
    if (first === null || first[0] !== BRIDGE_READY) {
      if (first !== null) process.kill('SIGTERM');
      const code = await process.exit;
      throw this.#bridgeError(code, stderr.text(), socketPath) ?? unexpectedEnd(socketPath);
    }
    return new ChannelStream(process, first.subarray(1), code =>
      this.#bridgeError(code, stderr.text(), socketPath),
    );
  }

  async close(): Promise<void> {
    await finish(
      this.#runner.spawn([...this.#common(), '-O', 'exit', '--', this.#options.alias], {}),
    );
  }

  async #establish(interactive: boolean): Promise<void> {
    const check = await finish(
      this.#runner.spawn([...this.#common(), '-O', 'check', '--', this.#options.alias], {}),
    );
    if (check.code === 0) return;
    const env: Record<string, string> = {};
    const batch = interactive ? [] : ['-o', 'BatchMode=yes'];
    if (interactive && this.#options.askpass !== undefined) {
      env['SSH_ASKPASS'] = this.#options.askpass;
      env['SSH_ASKPASS_REQUIRE'] = 'force';
    }
    const master = await finish(
      this.#runner.spawn(
        [
          ...this.#common(),
          ...batch,
          '-T',
          '-o',
          'ControlMaster=auto',
          '-o',
          'ControlPersist=30m',
          '-o',
          'ConnectTimeout=20',
          '--',
          this.#options.alias,
          'true',
        ],
        env,
      ),
    );
    if (master.code === 0) return;
    throw this.#sshError(master.stderr);
  }

  #resolve(): Promise<Resolved> {
    this.#resolved ??= this.#probe().catch((error: unknown) => {
      this.#resolved = null;
      throw error;
    });
    return this.#resolved;
  }

  async #probe(): Promise<Resolved> {
    const command = this.#options.vibesysCommand;
    const output = await finish(this.#channel(PROBE_SCRIPT, 'vibesys-probe', [command]));
    if (output.code === SSH_FAILED) throw this.#sshError(output.stderr);
    const lines = output.stdout.split('\n');
    const found = lines.find(line => line.startsWith('found '))?.slice('found '.length);
    const first = command.split(' ')[0] ?? command;
    if (output.code !== 0 || found === undefined) {
      const tried = [
        'the PATH of a non-interactive shell',
        ...PROBED_DIRECTORIES.map(d => `${d}/${first}`),
      ];
      throw new HostError(
        'failed',
        `the vibesys command "${command}" was not found on ${this.#options.alias}; tried ` +
          `${tried.join(', ')}. Set the host's vibesys command to a full path.`,
      );
    }
    const rest = command.slice(first.length);
    const vibesys = `${shellQuote(found)}${rest}`;
    const head = lines.find(line => line.startsWith('first '))?.slice('first '.length) ?? '';
    const shebang = head.startsWith('#!') ? head.slice(2) : '';
    const python = this.#options.pythonCommand ?? derivePython(vibesys, rest, shebang);
    if (python === null) {
      const problem =
        `cannot tell which Python runs "${command}" on ${this.#options.alias}` +
        (shebang === '' ? '' : ` (its first line is "#!${shebang.trim()}")`) +
        "; set the host's Python command in the picker.";
      return {vibesys, python: {problem}};
    }
    return {vibesys, python: {command: python}};
  }

  #channel(script: string, role: string, args: readonly string[]): SpawnedProcess {
    return this.#runner.spawn(
      [
        ...this.#common(),
        '-T',
        '-o',
        'ControlMaster=no',
        '-o',
        'BatchMode=yes',
        '--',
        this.#options.alias,
        remoteCommand(script, role, args),
      ],
      {},
    );
  }

  #common(): string[] {
    return [
      '-o',
      `ControlPath=${this.#options.controlPath}`,
      '-o',
      'ServerAliveInterval=15',
      '-o',
      'ServerAliveCountMax=3',
    ];
  }

  /**
   * What an ssh status 255 means: the host refused the user, or does not yet have a trusted host
   * key (both `auth`, which only an interactive sign-in can answer), or the link is down.
   */
  #sshError(stderr: string): HostError {
    if (AUTH_FAILURE.test(stderr)) {
      return new HostError('auth', `ssh ${this.#options.alias}: ${lastLine(stderr)}`);
    }
    return this.#linkError(stderr);
  }

  #linkError(stderr: string): HostError {
    return new HostError(
      'link',
      `the connection to ${this.#options.alias} is down: ${lastLine(stderr)}`,
    );
  }

  #bridgeError(code: number | null, stderr: string, socketPath: string): HostError | null {
    // A channel killed by a signal lost its link; a status the bridge does not define is a failure.
    const kind: HostErrorKind | null =
      code === null ? 'link' : BRIDGE_EXITS.has(code) ? (BRIDGE_EXITS.get(code) ?? null) : 'failed';
    if (kind === null) return null;
    const report = bridgeReport(stderr);
    if (kind === 'unreachable') {
      return new HostError(
        'unreachable',
        `nothing is listening at ${socketPath}${report === null ? '' : ` (${report})`}`,
      );
    }
    if (code === SSH_FAILED) return this.#sshError(stderr);
    if (kind === 'link' && code === null) return this.#linkError(stderr);
    return new HostError(
      kind,
      `the bridge to ${socketPath} on ${this.#options.alias} ended: ` +
        (report ?? `status ${code}: ${lastLine(stderr)}`),
    );
  }
}

/**
 * ssh failures only the user can answer: refused credentials, or a host key that is not trusted yet
 * (BatchMode cannot ask; an interactive sign-in asks through the askpass dialog, never silently).
 */
const AUTH_FAILURE =
  /Permission denied|Too many authentication failures|Authentication failed|Host key verification failed/;

/**
 * The Python for a vibesys command: a launcher like `uv run ... vibesys` runs `uv run ... python`;
 * an installed script runs the interpreter of its `#!` line when that is a Python (directly or
 * through `env`). Null otherwise: a `#!/bin/sh` launcher (uv's polyglot scripts) names no Python.
 */
function derivePython(vibesys: string, rest: string, shebang: string): string | null {
  const words = rest.trim().split(/\s+/);
  if (rest.trim() !== '' && words.at(-1) === 'vibesys') {
    return `${vibesys.slice(0, vibesys.length - 'vibesys'.length)}python`;
  }
  const interpreter = shebang.trim().split(/\s+/);
  const program = interpreter[0]?.endsWith('/env') ? interpreter[1] : interpreter[0];
  const name = program?.split('/').at(-1) ?? '';
  return /^python[0-9.]*$/.test(name) ? interpreter.map(shellQuote).join(' ') : null;
}

/**
 * The stdio bridge's report, `outcome: detail`, from the one JSON line it writes to standard error
 * on a nonzero exit (`{"outcome", "exit_status", "detail"}`); null when there is none.
 */
function bridgeReport(stderr: string): string | null {
  for (const line of stderr.split('\n').reverse()) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line) as unknown;
    } catch {
      continue;
    }
    if (typeof parsed !== 'object' || parsed === null) continue;
    const {outcome, detail} = parsed as {outcome?: unknown; detail?: unknown};
    if (typeof outcome !== 'string') continue;
    return typeof detail === 'string' && detail !== '' ? `${outcome}: ${detail}` : outcome;
  }
  return null;
}

function lastLine(text: string): string {
  return (
    text
      .split('\n')
      .map(line => line.trim())
      .filter(line => line !== '')
      .at(-1) ?? 'no detail'
  );
}

function unexpectedEnd(socketPath: string): HostError {
  return new HostError('failed', `the bridge to ${socketPath} ended before it was ready`);
}

/** The first chunk `process` writes to stdout, or null when it ends without writing. */
function firstChunk(process: SpawnedProcess): Promise<Buffer | null> {
  return new Promise(resolve => {
    const onData = (chunk: Buffer): void => {
      cleanup();
      process.stdout.pause();
      resolve(chunk);
    };
    const onEnd = (): void => {
      cleanup();
      resolve(null);
    };
    const cleanup = (): void => {
      process.stdout.off('data', onData);
      process.stdout.off('end', onEnd);
    };
    process.stdout.on('data', onData);
    process.stdout.once('end', onEnd);
  });
}

/** The last few kilobytes a stream wrote. */
class Tail {
  #text = '';

  push(chunk: string): void {
    this.#text = `${this.#text}${chunk}`.slice(-4096);
  }

  text(): string {
    return this.#text;
  }
}

/** One ssh channel as a byte stream: stdin is its writable side, stdout its readable side. */
class ChannelStream extends Duplex {
  readonly #process: SpawnedProcess;

  constructor(
    process: SpawnedProcess,
    first: Buffer,
    endError: (code: number | null) => HostError | null,
  ) {
    super({allowHalfOpen: false});
    this.#process = process;
    if (first.length > 0) this.push(first);
    process.stdout.on('data', (chunk: Buffer) => {
      if (!this.push(chunk)) process.stdout.pause();
    });
    const ended = (): void => {
      void process.exit.then(code => {
        const error = endError(code);
        if (error === null) this.push(null);
        else this.destroy(error);
      });
    };
    // A channel whose whole output was its first chunk may have ended already (`firstChunk`
    // pauses after that chunk, but the end is still emitted), and would otherwise never end.
    if (process.stdout.readableEnded) ended();
    else process.stdout.once('end', ended);
    process.stdout.resume();
  }

  override _read(): void {
    this.#process.stdout.resume();
  }

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: () => void): void {
    // A channel that ended reports why through its exit, not through this write.
    this.#process.stdin.write(chunk, () => callback());
  }

  override _final(callback: () => void): void {
    this.#process.stdin.end();
    callback();
  }

  override _destroy(error: Error | null, callback: (error: Error | null) => void): void {
    this.#process.stdin.destroy();
    this.#process.kill('SIGTERM');
    callback(error);
  }
}

/** The real `ssh` client, with this process's environment plus `env`. */
const nodeRunner: SshRunner = {
  spawn: (args, env) =>
    fromChild(spawn('ssh', args, {stdio: 'pipe', env: {...process.env, ...env}})),
};
