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
 */
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import * as Ref from 'effect/Ref';
import type * as Scope from 'effect/Scope';
import * as Semaphore from 'effect/Semaphore';
import {ESTABLISH_BOUND_MS} from '../state/gateway-policy.js';
import {remoteCommand} from '../state/remote-command.js';
import {LinkFailed, type SpawnFailed} from './errors.js';
import {ProcessSpawner, retire, type SpawnedProcess, spawnScoped} from './process.js';

export interface SshLinkOptions {
  /** The `Host` alias from `~/.ssh/config` (validated as a host key before it gets here). */
  readonly alias: string;
  /** The master connection's socket path on this machine; short (Unix sockets cap paths). */
  readonly controlPath: string;
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
  /** End the master and any attempt under way. Idempotent; every caller sees the same outcome. */
  readonly release: Effect.Effect<void, LinkFailed>;
}

/** Every control command is killed after this bound. */
const CONTROL_COMMAND_BOUND = '1 second';
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
  readonly stderr: string;
}

/** Start `ssh` with the link's options; the caller retires the process. */
type Ssh = (args: readonly string[]) => Effect.Effect<SpawnedProcess, SpawnFailed>;

const linkDown = (message: string): LinkFailed => new LinkFailed({problem: 'link', message});

/** Run a short ssh command to its end with stdin closed. */
const finish = (process: SpawnedProcess): Effect.Effect<Finished> =>
  Effect.suspend(() => {
    let stderr = '';
    process.stdin.end();
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-STDERR_TAIL_BYTES);
    });
    process.stdout.resume();
    return Effect.map(process.exit, code => ({code, stderr}));
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
const runShort = (ssh: Ssh, args: readonly string[]): Effect.Effect<Finished, LinkFailed> =>
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

/** One attempt to bring the master up: nothing to do when it already answers `-O check`. */
const bringUp = (ssh: Ssh, alias: string): Effect.Effect<void, LinkFailed> =>
  Effect.gen(function* () {
    const check = yield* runShort(ssh, ['-O', 'check', '--', alias]);
    if (check.code === 0) return;
    const master = yield* runShort(ssh, [...MASTER_OPTIONS, '--', alias, 'true']);
    if (master.code !== 0) return yield* Effect.fail(sshError(alias, master.stderr));
  });

/** A control command that must be seen to end: killed after the bound, then given one more. */
const control = (ssh: Ssh, args: readonly string[]) =>
  Effect.scoped(
    Effect.gen(function* () {
      const process = yield* Effect.acquireRelease(ssh(args), started =>
        Effect.asVoid(retire(started)),
      );
      const done = yield* Effect.timeoutOption(finish(process), CONTROL_COMMAND_BOUND);
      return Option.isSome(done)
        ? {confirmed: true, code: done.value.code}
        : {confirmed: (yield* retire(process)) === 'confirmed', code: null};
    }),
  ).pipe(Effect.catchTag('SpawnFailed', () => Effect.succeed({confirmed: false, code: null})));

/** End the master, and prove it is gone. */
const retireMaster = (ssh: Ssh, alias: string): Effect.Effect<void, LinkFailed> =>
  Effect.gen(function* () {
    const unconfirmed = linkDown('could not confirm teardown of the SSH master');
    const exit = yield* control(ssh, ['-O', 'exit', '--', alias]);
    if (!exit.confirmed) return yield* Effect.fail(unconfirmed);
    if (exit.code === 0) return;
    const check = yield* control(ssh, ['-O', 'check', '--', alias]);
    if (!check.confirmed || check.code === null) return yield* Effect.fail(unconfirmed);
    if (check.code === 0)
      return yield* Effect.fail(linkDown('the SSH master remained reachable after teardown'));
  });

export const makeSshLink = Effect.fn('SshLink.make')(function* (options: SshLinkOptions) {
  const spawner = yield* ProcessSpawner;
  const {alias} = options;
  const common = [
    '-o',
    `ControlPath=${options.controlPath}`,
    '-o',
    'ServerAliveInterval=15',
    '-o',
    'ServerAliveCountMax=3',
  ];
  const ssh: Ssh = args => spawner.spawn('ssh', [...common, ...args], {});

  const abandoned = Deferred.makeUnsafe<void>();
  const attempts = yield* Semaphore.make(1);
  const releasedError = linkDown(`the connection to ${alias} was released`);

  const establish = Effect.suspend(() =>
    Deferred.isDoneUnsafe(abandoned)
      ? Effect.fail(releasedError)
      : attempts.withPermit(
          Effect.raceFirst(
            bringUp(ssh, alias).pipe(
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

  const releasing = Deferred.makeUnsafe<void, LinkFailed>();
  const started = yield* Ref.make(false);
  const release = Effect.gen(function* () {
    if (yield* Ref.getAndSet(started, true)) return yield* Deferred.await(releasing);
    yield* Deferred.succeed(abandoned, undefined);
    // An attempt interrupted by the release has unwound once it gives the permit back.
    yield* attempts
      .withPermit(retireMaster(ssh, alias))
      .pipe(Effect.onExit(exit => Deferred.done(releasing, exit)));
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
