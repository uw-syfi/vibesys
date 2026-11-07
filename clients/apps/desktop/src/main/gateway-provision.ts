/**
 * Putting the gateway executable on a host over its ssh link, and running commands there. A
 * channel runs a script with `/bin/sh -c`; the scripts below contain only generated hex, integers
 * and fixed paths. The artifact is verified by size and SHA-256 before it is moved into place, so
 * a broken upload never replaces a working gateway.
 */
import {createHash, randomUUID} from 'node:crypto';
import type {Writable} from 'node:stream';
import {promisify} from 'node:util';
import {gzip} from 'node:zlib';
import {API_SCHEMA_HASH, DesktopRefusal} from '@vibesys/proto';
import * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import {COMMAND_BOUND_MS, COPY_STALL_MS} from '../state/gateway-policy.js';
import {
  missingPrerequisites,
  PREREQUISITE_SCRIPT,
  prerequisiteMessage,
} from '../state/host-prerequisites.js';
import {type GatewayTarget, gatewayTarget} from '../state/gateway-target.js';
import {uploadSchedule} from '../state/upload-pacing.js';
import {GatewayFailed, GatewayRefused, type LinkFailed, type SpawnFailed} from './errors.js';
import type {SpawnedProcess} from './process.js';
import type {SshLink} from './ssh-link.js';

/** Host executable name, pinned across languages by the host-program golden. */
export const HOST_PROGRAM = 'vibesys-host';

const compress = promisify(gzip);

/** Where a host keeps the gateway for this build, relative to its home directory. */
export const GATEWAY_DIRECTORY = `.vibesys/gateway/${API_SCHEMA_HASH.slice(0, 16)}`;

/**
 * The gateway executables this build ships, one per platform. A platform the build holds none for
 * is a `GatewayRefused` naming the platform and the version, which no retry changes; one it holds
 * and cannot read is a `GatewayFailed`.
 */
export class GatewayArtifacts extends Context.Service<
  GatewayArtifacts,
  {
    readonly read: (
      target: GatewayTarget,
    ) => Effect.Effect<Uint8Array, GatewayFailed | GatewayRefused>;
  }
>()('app/GatewayArtifacts') {}

const OUTPUT_LIMIT = 8192;
const STDERR_TAIL = 4096;

/** One write to `stdin`: whether the chunk was accepted (false when the pipe broke). */
const write = (stdin: Writable, chunk: Uint8Array): Effect.Effect<boolean> =>
  Effect.callback<boolean>(resume => {
    stdin.write(chunk, error => resume(Effect.succeed(error === null || error === undefined)));
  });

/**
 * Write `bytes` to `stdin` at a capped rate, one outstanding write at a time, then end it. The copy
 * is measured by progress: each chunk must be accepted within `COPY_STALL_MS`, so a copy of any
 * length that keeps moving is never cut, and one that stops is, after one stall window. The
 * pacing wait between chunks is not progress to wait for and is not counted. A broken pipe ends
 * the copy quietly: the process that closed it says why through its exit.
 */
const upload = (stdin: Writable, bytes: Uint8Array): Effect.Effect<void, GatewayFailed> =>
  Effect.gen(function* () {
    for (const step of uploadSchedule(bytes.length)) {
      const chunk = bytes.subarray(step.offset, step.offset + step.length);
      yield* Effect.sleep(`${step.delayMs} millis`);
      const accepted = yield* write(stdin, chunk).pipe(
        Effect.timeoutOrElse({
          duration: `${COPY_STALL_MS} millis`,
          orElse: () =>
            Effect.fail(
              new GatewayFailed({
                message: `gateway upload stalled: ${step.offset} of ${bytes.length} bytes sent, none accepted for ${COPY_STALL_MS / 1000} seconds`,
              }),
            ),
        }),
      );
      if (!accepted) return;
    }
    yield* Effect.sync(() => stdin.end());
  });

/** What a short channel command printed; fails with the link's words when the channel did. */
export const runCommand = (
  link: Pick<SshLink, 'channel' | 'explain'>,
  script: string,
  input?: Uint8Array,
): Effect.Effect<string, GatewayFailed | LinkFailed> =>
  Effect.scoped(
    Effect.gen(function* () {
      const process: SpawnedProcess = yield* link
        .channel(script)
        .pipe(Effect.mapError((error: SpawnFailed) => new GatewayFailed({message: error.message})));
      let output = '';
      let stderr = '';
      process.stdout.setEncoding('utf8');
      process.stderr.setEncoding('utf8');
      process.stdout.on('data', (chunk: string) => {
        if (output.length <= OUTPUT_LIMIT) output += chunk;
      });
      process.stderr.on('data', (chunk: string) => {
        stderr = `${stderr}${chunk}`.slice(-STDERR_TAIL);
      });
      process.stdin.on('error', () => {});
      const delivered =
        input === undefined ? Effect.sync(() => process.stdin.end()) : upload(process.stdin, input);
      // The command's own bound starts when it has all its input: the copy is bounded by its
      // progress (`upload`), the remote work after it by `COMMAND_BOUND_MS`. A process that ends
      // earlier ends the wait, whatever the copy was doing.
      const bounded = process.exit.pipe(Effect.timeoutOption(`${COMMAND_BOUND_MS} millis`));
      const code = yield* Effect.raceFirst(
        Effect.map(process.exit, Option.some),
        Effect.andThen(delivered, () => bounded),
      );
      if (Option.isNone(code))
        return yield* new GatewayFailed({message: 'gateway command timed out'});
      if (code.value !== 0) return yield* link.explain(code.value, stderr, 'gateway command');
      if (output.length > OUTPUT_LIMIT)
        return yield* new GatewayFailed({
          message: `gateway command output exceeds ${OUTPUT_LIMIT} bytes`,
        });
      return output;
    }),
  );

/**
 * Make sure the host holds this build's gateway: a matching executable stays; otherwise (or when
 * `force`) the artifact is uploaded, verified and moved into place.
 */
export const provisionGateway = (
  link: Pick<SshLink, 'channel' | 'explain'>,
  force: boolean,
): Effect.Effect<void, GatewayFailed | GatewayRefused | LinkFailed, GatewayArtifacts> =>
  Effect.gen(function* () {
    const run = (script: string, input?: Uint8Array) => runCommand(link, script, input);
    /**
     * Refuse a host that lacks something the install needs, naming it. One check covers the whole
     * table (`state/host-prerequisites.ts`); a host that has everything passes through.
     */
    const requirePrerequisites = Effect.gen(function* () {
      const missing = missingPrerequisites(yield* run(PREREQUISITE_SCRIPT));
      if (missing.length > 0)
        return yield* new GatewayRefused({
          refusal: DesktopRefusal.HOST_PREREQUISITE_MISSING,
          message: prerequisiteMessage(missing),
        });
    });
    const platform = yield* run('uname -s; uname -m');
    const target = yield* Effect.try({
      try: () => gatewayTarget(platform),
      catch: cause => cause,
    }).pipe(
      // A platform probe that says nothing usable may be a host without `uname`: say that first.
      Effect.catch(cause =>
        requirePrerequisites.pipe(
          Effect.andThen(
            Effect.fail(
              new GatewayRefused({
                refusal: DesktopRefusal.UNSUPPORTED_PLATFORM,
                message: String(cause instanceof Error ? cause.message : cause),
              }),
            ),
          ),
        ),
      ),
    );
    const artifacts = yield* GatewayArtifacts;
    const bytes = yield* artifacts.read(target);
    const digest = createHash('sha256').update(bytes).digest('hex');
    const binary = `"$HOME/${GATEWAY_DIRECTORY}/${HOST_PROGRAM}"`;
    const matches = (path: string) =>
      `[ -f ${path} ] && [ "$(wc -c < ${path} | tr -d ' ')" = '${bytes.length}' ] && ` +
      `[ "$(if command -v sha256sum >/dev/null 2>&1; then sha256sum ${path}; else shasum -a 256 ${path}; fi | cut -d ' ' -f 1)" = '${digest}' ]`;
    const holds = (path: string) => run(`if ${matches(path)}; then printf match; fi`);
    if (!force && (yield* holds(binary)) === 'match') return;
    // Everything the copy runs is checked before it starts: a host that lacks one of them would
    // fail the install with an exit code, which reads as a lost link and is redialed for ever.
    yield* requirePrerequisites;
    const temporary = `"$HOME/${GATEWAY_DIRECTORY}/.${HOST_PROGRAM}-${randomUUID()}"`;
    const packed = yield* Effect.tryPromise({
      try: () => compress(bytes),
      catch: cause =>
        new GatewayFailed({message: `could not compress the gateway: ${String(cause)}`}),
    });
    yield* run(
      `umask 077; mkdir -p "$HOME/${GATEWAY_DIRECTORY}" && ` +
        `trap 'rm -f ${temporary}' EXIT HUP INT TERM; ` +
        `gzip -dc > ${temporary} && ${matches(temporary)} && ` +
        `chmod 700 ${temporary} && mv -f ${temporary} ${binary}`,
      packed,
    );
    if ((yield* holds(binary)) !== 'match')
      return yield* new GatewayFailed({
        message: `gateway artifact verification failed for ${target}`,
      });
  });

/** The command that runs the gateway's stdio bridge on a host. */
export const GATEWAY_CONNECT_SCRIPT = `exec "$HOME/${GATEWAY_DIRECTORY}/${HOST_PROGRAM}" connect`;
