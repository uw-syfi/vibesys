/**
 * How the main process reaches one ssh host: the system OpenSSH client, so the user's
 * `~/.ssh/config`, ProxyJump, agent, certificates and Kerberos work unchanged. The app owns one
 * master connection per host (`ControlMaster`/`ControlPath`/`ControlPersist`); every channel is an
 * exec session over that master with `BatchMode=yes`, so a channel never prompts. The master is
 * brought up non-interactively too: a host that wants a password or a new host key fails as `auth`
 * with ssh's own words, and the user fixes it in their terminal.
 *
 * The master carries `ServerAliveInterval`, so a half-open link ends instead of hanging, and
 * `ControlPersist=30m`, so a master an app crash left behind ends on its own.
 *
 * A master can freeze hard enough to ignore its own control socket, so that `-O exit` and `-O
 * check` wait forever. The link learns the master's pid when it brings the master up, and a control
 * command that does not answer within its bound escalates: the process is killed by that pid, once
 * it is confirmed to still be an ssh master, and the next attempt starts a fresh one.
 */
import * as Deferred from 'effect/Deferred';
import type * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Option from 'effect/Option';
import type * as Scope from 'effect/Scope';
import * as Semaphore from 'effect/Semaphore';
import {ESTABLISH_BOUND_MS} from '../state/gateway-policy.js';
import {remoteCommand} from '../state/remote-command.js';
import {LinkFailed, type SpawnFailed} from './errors.js';
import {ProcessSpawner, retire, type SpawnedProcess, spawnScoped} from './process.js';

/**
 * What is known of one host's master across the links made for it: the pid it reported. A link is
 * made per lease cycle, and a master that froze in one cycle must still be killable in the next.
 */
export interface MasterCell {
  pid: number | undefined;
}

export interface SshLinkOptions {
  /** The `Host` alias from `~/.ssh/config` (validated as a host key before it gets here). */
  readonly alias: string;
  /** The master connection's socket path on this machine; short (Unix sockets cap paths). */
  readonly controlPath: string;
  /** Where the master's pid is kept; a link made without one keeps it for its own life. */
  readonly master?: MasterCell;
}

/** One host's ssh link. */
export interface SshLink {
  readonly alias: string;
  /**
   * Bring the master up if it is down; one attempt at a time. Fails with `LinkFailed`: `auth` when
   * the host refused the user, `capacity` when the host's session ceiling is spent, `link` when it
   * is unreachable. Interrupting a caller ends its attempt.
   */
  readonly establish: Effect.Effect<void, LinkFailed>;
  /** A channel over the master that runs `script` with `/bin/sh -c`, retired with the scope. */
  readonly channel: (script: string) => Effect.Effect<SpawnedProcess, SpawnFailed, Scope.Scope>;
  /** Why a channel's process for `where` ended, in the words of the link. */
  readonly explain: (code: number | null, stderr: string, where: string) => LinkFailed;
  /**
   * End the master and any attempt under way. Concurrent callers share one attempt and see its
   * outcome. An attempt that failed leaves the master possibly alive, so the next call tries again.
   */
  readonly release: Effect.Effect<void, LinkFailed>;
}

/** A teardown command (`-O exit`, a kill, a look at a pid) is killed after this bound. */
const CONTROL_COMMAND_BOUND = '1 second';
/**
 * How long `-O check` may take before the master counts as frozen. A healthy master answers in
 * milliseconds; this is generous because killing a master that was only slow breaks its channels.
 */
const CHECK_BOUND = '5 seconds';
const STDERR_TAIL_BYTES = 4096;

/** ssh's own exit status for a connection or authentication failure. */
const SSH_FAILED = 255;

/**
 * ssh failures only the user can answer: refused credentials, or a host key that is not trusted yet
 * (BatchMode cannot ask).
 */
const AUTH_FAILURE =
  /Permission denied|Too many authentication failures|Authentication failed|Host key verification failed/;

/**
 * The host refused a new channel because this app's other channels to it already spent its session
 * ceiling (sshd's `MaxSessions`, default 10). `mux_client_request_session: ... Session open refused
 * by peer` is the OpenSSH mux client's wording for exactly this refusal over the shared
 * `ControlMaster`; `administratively prohibited` is the server-side reason it can surface as.
 * Neither is a link problem a retry fixes: the link and the host are fine, only this app's own
 * channel count against this one host is not.
 */
const SESSION_CAPACITY_REFUSED = /session request failed|administratively prohibited/i;

const lastLine = (text: string): string =>
  text
    .split('\n')
    .map(line => line.trim())
    .filter(line => line !== '')
    .at(-1) ?? 'no detail';

interface Finished {
  readonly code: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

/** Start a program with arguments; the caller retires the process. */
type Run = (args: readonly string[]) => Effect.Effect<SpawnedProcess, SpawnFailed>;

const linkDown = (message: string): LinkFailed => new LinkFailed({problem: 'link', message});

/** Run a short command to its end with stdin closed, reading what it printed. */
const finish = (process: SpawnedProcess): Effect.Effect<Finished> =>
  Effect.suspend(() => {
    let stdout = '';
    let stderr = '';
    process.stdin.end();
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-STDERR_TAIL_BYTES);
    });
    process.stdout.setEncoding('utf8');
    process.stdout.on('data', (chunk: string) => {
      stdout = `${stdout}${chunk}`.slice(-STDERR_TAIL_BYTES);
    });
    return Effect.map(process.exit, code => ({code, stdout, stderr}));
  });

/** An ssh status 255: the user was refused, the session ceiling is spent, or the link is down. */
const sshError = (alias: string, stderr: string): LinkFailed => {
  if (SESSION_CAPACITY_REFUSED.test(stderr)) {
    return new LinkFailed({
      problem: 'capacity',
      message: `too many runs are already open against ${alias}; close one or wait, then retry (${lastLine(stderr)})`,
    });
  }
  if (AUTH_FAILURE.test(stderr))
    return new LinkFailed({problem: 'auth', message: `ssh ${alias}: ${lastLine(stderr)}`});
  return linkDown(`the connection to ${alias} is down: ${lastLine(stderr)}`);
};

/** Run one ssh command to its end; the process is retired with the call. */
const runShort = (ssh: Run, args: readonly string[]): Effect.Effect<Finished, LinkFailed> =>
  Effect.scoped(
    Effect.gen(function* () {
      const process = yield* Effect.acquireRelease(ssh(args), started =>
        Effect.asVoid(retire(started)),
      );
      return yield* finish(process);
    }),
  ).pipe(Effect.mapError(error => linkDown(`could not start ssh: ${error.message}`)));

/** The options that bring the master up: non-interactive, persistent, compressed. */
const MASTER_OPTIONS = [
  '-o',
  'BatchMode=yes',
  '-T',
  '-o',
  'ControlMaster=auto',
  '-o',
  'ControlPersist=30m',
  '-o',
  'ConnectTimeout=20',
  // Every channel is a session on this master, so this compresses all of them. A run's journal is
  // text that shrinks 8-20x.
  '-o',
  'Compression=yes',
];

/** What a control command did: it ended within its bound (`code`), or was killed for not ending. */
interface Controlled {
  /** The command is proven gone: it ended, or it was killed and confirmed dead. */
  readonly confirmed: boolean;
  /** Its exit code; null when it did not end within its bound, which for a control command means that the master is not answering. */
  readonly code: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

const unanswered: Controlled = {confirmed: false, code: null, stdout: '', stderr: ''};

/** A command that must be seen to end: killed after `bound`, then given one more. */
const control = (
  run: Run,
  args: readonly string[],
  bound: Duration.Input = CONTROL_COMMAND_BOUND,
): Effect.Effect<Controlled> =>
  Effect.scoped(
    Effect.gen(function* () {
      const process = yield* Effect.acquireRelease(run(args), started =>
        Effect.asVoid(retire(started)),
      );
      const done = yield* Effect.timeoutOption(finish(process), bound);
      return Option.isSome(done)
        ? ({confirmed: true, ...done.value} satisfies Controlled)
        : ({...unanswered, confirmed: (yield* retire(process)) === 'confirmed'} satisfies Controlled);
    }),
  ).pipe(Effect.catchTag('SpawnFailed', () => Effect.succeed(unanswered)));

/** The pid ssh's `-O check` reports on stderr: `Master running (pid=1234)`. */
const MASTER_PID = /\(pid=(\d+)\)/;

const pidOf = (stderr: string): number | undefined => {
  const found = MASTER_PID.exec(stderr)?.[1];
  return found === undefined ? undefined : Number(found);
};

/** What ssh titles a master's process: `ssh: /path/to/socket [mux]`. */
const MASTER_TITLE = /\[mux\]/;

/** The programs that look at and end a process, started like ssh so a test can stand in for them. */
interface Tools {
  readonly ps: Run;
  readonly kill: Run;
}

/**
 * Kill the master by the pid it reported, once that pid is still an ssh master (a pid is reused
 * when its process ends), and prove that it no longer answers. True when the master is gone; false
 * when it could not be killed or its pid is not known, which leaves the teardown unconfirmed.
 */
const killMaster = (
  ssh: Run,
  tools: Tools,
  alias: string,
  cell: MasterCell,
): Effect.Effect<boolean> =>
  Effect.gen(function* () {
    const pid = cell.pid;
    if (pid === undefined) return false;
    const seen = yield* control(tools.ps, ['-p', String(pid), '-o', 'command=']);
    if (!seen.confirmed || seen.code === null) return false;
    // No longer an ssh master: it has ended, and the pid is someone else's now.
    if (!MASTER_TITLE.test(seen.stdout)) {
      cell.pid = undefined;
      return true;
    }
    const killed = yield* control(tools.kill, ['-KILL', String(pid)]);
    if (!killed.confirmed || killed.code !== 0) return false;
    const check = yield* control(ssh, ['-O', 'check', '--', alias]);
    if (!check.confirmed || check.code === null || check.code === 0) return false;
    cell.pid = undefined;
    return true;
  });

/**
 * One attempt to bring the master up: nothing to do when it already answers `-O check`. A master
 * that does not answer within the bound is frozen: it is killed and a fresh one started.
 */
const bringUp = (
  ssh: Run,
  tools: Tools,
  alias: string,
  cell: MasterCell,
): Effect.Effect<void, LinkFailed> =>
  Effect.gen(function* () {
    const check = yield* control(ssh, ['-O', 'check', '--', alias], CHECK_BOUND);
    if (check.code === 0) {
      cell.pid = pidOf(check.stderr) ?? cell.pid;
      return;
    }
    if (check.code === null && !(yield* killMaster(ssh, tools, alias, cell)))
      return yield* Effect.fail(
        linkDown(`the master of ${alias} does not answer and could not be ended`),
      );
    const master = yield* runShort(ssh, [...MASTER_OPTIONS, '--', alias, 'true']);
    if (master.code !== 0) return yield* Effect.fail(sshError(alias, master.stderr));
    // Its pid is what a later teardown kills if the master freezes.
    const alive = yield* control(ssh, ['-O', 'check', '--', alias]);
    cell.pid = alive.code === 0 ? pidOf(alive.stderr) : undefined;
  });

/** End the master, and prove it is gone: by `-O exit`, or when it will not answer, by its pid. */
const retireMaster = (
  ssh: Run,
  tools: Tools,
  alias: string,
  cell: MasterCell,
): Effect.Effect<void, LinkFailed> =>
  Effect.gen(function* () {
    const unconfirmed = linkDown('could not confirm teardown of the SSH master');
    const frozen = Effect.flatMap(killMaster(ssh, tools, alias, cell), killed =>
      killed ? Effect.void : Effect.fail(unconfirmed),
    );
    const exit = yield* control(ssh, ['-O', 'exit', '--', alias]);
    if (exit.confirmed && exit.code === 0) {
      cell.pid = undefined;
      return;
    }
    if (exit.code === null) return yield* frozen;
    if (!exit.confirmed) return yield* Effect.fail(unconfirmed);
    const check = yield* control(ssh, ['-O', 'check', '--', alias]);
    if (check.code === null) return yield* frozen;
    if (!check.confirmed) return yield* Effect.fail(unconfirmed);
    if (check.code === 0)
      return yield* Effect.fail(linkDown('the SSH master remained reachable after teardown'));
    cell.pid = undefined;
  });

export const makeSshLink = Effect.fn('SshLink.make')(function* (options: SshLinkOptions) {
  const spawner = yield* ProcessSpawner;
  const {alias} = options;
  const cell: MasterCell = options.master ?? {pid: undefined};
  const common = [
    '-o',
    `ControlPath=${options.controlPath}`,
    '-o',
    'ServerAliveInterval=15',
    '-o',
    'ServerAliveCountMax=3',
  ];
  const ssh: Run = args => spawner.spawn('ssh', [...common, ...args], {});
  const tools: Tools = {
    ps: args => spawner.spawn('ps', args, {}),
    kill: args => spawner.spawn('kill', args, {}),
  };

  const abandoned = Deferred.makeUnsafe<void>();
  const attempts = yield* Semaphore.make(1);
  const releasedError = linkDown(`the connection to ${alias} was released`);

  const establish = Effect.suspend(() =>
    Deferred.isDoneUnsafe(abandoned)
      ? Effect.fail(releasedError)
      : attempts.withPermit(
          Effect.raceFirst(
            bringUp(ssh, tools, alias, cell).pipe(
              Effect.timeoutOrElse({
                duration: `${ESTABLISH_BOUND_MS} millis`,
                orElse: () =>
                  Effect.fail(
                    linkDown(
                      `bringing the connection to ${alias} up took longer than ${ESTABLISH_BOUND_MS / 1000} seconds`,
                    ),
                  ),
              }),
            ),
            Effect.andThen(
              Deferred.await(abandoned),
              Effect.fail(linkDown('the connection attempt was abandoned')),
            ),
          ),
        ),
  );

  /** The attempt at releasing that is under way, shared by every caller of it. */
  let releasing: Deferred.Deferred<void, LinkFailed> | undefined;
  const release = Effect.suspend(() => {
    if (releasing !== undefined) return Deferred.await(releasing);
    const done = Deferred.makeUnsafe<void, LinkFailed>();
    releasing = done;
    return Effect.gen(function* () {
      yield* Deferred.succeed(abandoned, undefined);
      // An attempt interrupted by the release has unwound once it gives the permit back.
      yield* attempts.withPermit(retireMaster(ssh, tools, alias, cell));
    }).pipe(
      Effect.onExit(exit => {
        // A failed release leaves the master possibly alive: the next call is a new attempt.
        if (Exit.isFailure(exit) && releasing === done) releasing = undefined;
        return Deferred.done(done, exit);
      }),
    );
  });

  const link: SshLink = {
    alias,
    establish,
    channel: script =>
      spawnScoped(
        'ssh',
        [
          ...common,
          '-T',
          '-o',
          'ControlMaster=no',
          '-o',
          'BatchMode=yes',
          '--',
          alias,
          remoteCommand(script),
        ],
        {},
      ).pipe(Effect.provideService(ProcessSpawner, spawner)),
    explain: (code, stderr, where) => {
      if (code === SSH_FAILED) return sshError(alias, stderr);
      if (code === null) return linkDown(`the channel to ${alias} broke`);
      return linkDown(`${where} exited with ${code}: ${lastLine(stderr)}`);
    },
    release,
  };
  return link;
});
