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
import {API_SCHEMA_HASH} from '@vibesys/proto';
import * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import * as Option from 'effect/Option';
import {type GatewayTarget, gatewayTarget} from '../state/gateway-target.js';
import {uploadSchedule} from '../state/upload-pacing.js';
import {GatewayFailed, type LinkFailed, type SpawnFailed} from './errors.js';
import type {SpawnedProcess} from './process.js';
import type {SshLink} from './ssh-link.js';

/** Host executable name, pinned across languages by the host-program golden. */
export const HOST_PROGRAM = 'vibesys-host';

const compress = promisify(gzip);

/** Where a host keeps the gateway for this build, relative to its home directory. */
export const GATEWAY_DIRECTORY = `.vibesys/gateway/${API_SCHEMA_HASH.slice(0, 16)}`;

/** The gateway executables this build ships, one per platform. */
export class GatewayArtifacts extends Context.Service<
  GatewayArtifacts,
  {readonly read: (target: GatewayTarget) => Effect.Effect<Uint8Array, GatewayFailed>}
>()('app/GatewayArtifacts') {}

const COMMAND_BOUND = '30 seconds';
// Cold provisioning transfers a multi-megabyte artifact.
const UPLOAD_BOUND = '90 seconds';
const OUTPUT_LIMIT = 8192;
const STDERR_TAIL = 4096;

/** Write `bytes` to `stdin` at a capped rate, one outstanding write at a time, then end it. */
const upload = (stdin: Writable, bytes: Uint8Array): Effect.Effect<void, GatewayFailed> =>
  Effect.gen(function* () {
    for (const step of uploadSchedule(bytes.length)) {
      const chunk = bytes.subarray(step.offset, step.offset + step.length);
      yield* Effect.sleep(`${step.delayMs} millis`);
      yield* Effect.callback<void, GatewayFailed>(resume => {
        stdin.write(chunk, error =>
          resume(
            error
              ? Effect.fail(new GatewayFailed({message: `gateway upload failed: ${error.message}`}))
              : Effect.void,
          ),
        );
      });
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
      yield* Effect.forkScoped(
        Effect.ignore(
          input === undefined
            ? Effect.sync(() => process.stdin.end())
            : upload(process.stdin, input),
        ),
      );
      const code = yield* process.exit.pipe(
        Effect.timeoutOption(input === undefined ? COMMAND_BOUND : UPLOAD_BOUND),
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
): Effect.Effect<void, GatewayFailed | LinkFailed, GatewayArtifacts> =>
  Effect.gen(function* () {
    const run = (script: string, input?: Uint8Array) => runCommand(link, script, input);
    const platform = yield* run('uname -s; uname -m');
    const target = yield* Effect.try({
      try: () => gatewayTarget(platform),
      catch: cause =>
        new GatewayFailed({message: String(cause instanceof Error ? cause.message : cause)}),
    });
    const artifacts = yield* GatewayArtifacts;
    const bytes = yield* artifacts.read(target);
    const digest = createHash('sha256').update(bytes).digest('hex');
    const binary = `"$HOME/${GATEWAY_DIRECTORY}/${HOST_PROGRAM}"`;
    const matches = (path: string) =>
      `[ -f ${path} ] && [ "$(wc -c < ${path} | tr -d ' ')" = '${bytes.length}' ] && ` +
      `[ "$(if command -v sha256sum >/dev/null 2>&1; then sha256sum ${path}; else shasum -a 256 ${path}; fi | cut -d ' ' -f 1)" = '${digest}' ]`;
    const holds = (path: string) => run(`if ${matches(path)}; then printf match; fi`);
    if (!force && (yield* holds(binary)) === 'match') return;
    const temporary = `"$HOME/${GATEWAY_DIRECTORY}/.${HOST_PROGRAM}-${randomUUID()}"`;
    const packed = yield* Effect.tryPromise({
      try: () => compress(bytes),
      catch: cause =>
        new GatewayFailed({message: `could not compress the gateway: ${String(cause)}`}),
    });
    yield* run(
      `umask 077; mkdir -p "$HOME/${GATEWAY_DIRECTORY}" && ` +
        `trap 'rm -f ${temporary}' EXIT HUP INT TERM; ` +
        `command -v gzip >/dev/null 2>&1 || { printf 'gateway provisioning requires gzip\\n' >&2; exit 1; }; ` +
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
