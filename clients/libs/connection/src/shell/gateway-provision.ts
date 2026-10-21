/** Artifact verification and atomic installation over the existing SSH command channel. */
import {createHash, randomUUID} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {Duplex} from 'node:stream';
import {API_SCHEMA_HASH} from '@vibesys/proto';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import type {SshLink} from './link.js';
import {retireProcess, type SpawnedProcess} from './process.js';

export type GatewayTarget = 'linux-amd64' | 'linux-arm64' | 'darwin-arm64';
export const GATEWAY_DIRECTORY = `.vibesys/gateway/${API_SCHEMA_HASH.slice(0, 16)}`;

/** Normalize platform names reported by either remote uname or local Node os APIs. */
export function gatewayTarget(output: string): GatewayTarget {
  const [os, arch] = output.trim().split(/\s+/);
  const target = `${os?.toLowerCase()}-${arch === 'x86_64' || arch === 'x64' ? 'amd64' : arch === 'aarch64' ? 'arm64' : arch}`;
  if (target === 'linux-amd64' || target === 'linux-arm64' || target === 'darwin-arm64')
    return target;
  throw new Error(`unsupported gateway platform: ${output.trim()}`);
}

/** Shell scripts below contain only generated hex, integers, and fixed paths. */
export async function provisionGateway(
  link: Pick<SshLink, 'spawnCommand'>,
  artifact: (target: GatewayTarget) => Promise<Uint8Array>,
  force: boolean,
  signal: AbortSignal,
  schedule: ScheduleTimeout,
): Promise<void> {
  const run = (script: string, input?: Uint8Array) =>
    command(link.spawnCommand(script), signal, schedule, input);
  const target = gatewayTarget(await run('uname -s; uname -m'));
  const bytes = await artifact(target);
  const digest = createHash('sha256').update(bytes).digest('hex');
  const binary = `"$HOME/${GATEWAY_DIRECTORY}/vibesysd"`;
  const matches =
    `[ -f ${binary} ] && [ "$(wc -c < ${binary} | tr -d ' ')" = '${bytes.length}' ] && ` +
    `[ "$(if command -v sha256sum >/dev/null 2>&1; then sha256sum ${binary}; else shasum -a 256 ${binary}; fi | cut -d ' ' -f 1)" = '${digest}' ]`;
  if (!force && (await run(`if ${matches}; then printf match; fi`)) === 'match') return;
  const temporary = `"$HOME/${GATEWAY_DIRECTORY}/.vibesysd-${randomUUID()}"`;
  await run(
    `umask 077; mkdir -p "$HOME/${GATEWAY_DIRECTORY}" && ` +
      `trap 'rm -f ${temporary}' EXIT HUP INT TERM; ` +
      `cat > ${temporary} && chmod 700 ${temporary} && mv -f ${temporary} ${binary}`,
    bytes,
  );
  if ((await run(`if ${matches}; then printf match; fi`)) !== 'match') {
    throw new Error(`gateway artifact verification failed for ${target}`);
  }
}

/** A missing build is fatal only when a caller has opted into the gateway. */
export async function gatewayArtifact(
  directory: string,
  target: GatewayTarget,
): Promise<Uint8Array> {
  try {
    return await readFile(`${directory}/${API_SCHEMA_HASH.slice(0, 16)}/vibesysd-${target}`);
  } catch (cause) {
    throw new Error(
      `Run transport v2 requires gateway resources; install Go and rebuild (${target})`,
      {cause},
    );
  }
}

/** Convert existing process pipes to a single owned duplex, honoring stream backpressure. */
export function gatewayProcess(process: SpawnedProcess, schedule: ScheduleTimeout): Duplex {
  const duplex = Duplex.from({readable: process.stdout, writable: process.stdin});
  process.stderr.resume();
  void process.exit.then(code => {
    if (!duplex.destroyed) duplex.destroy(new Error(`gateway connect exited: ${code}`));
  });
  duplex.once('close', () => {
    void retireProcess(process, schedule).outcome;
  });
  return duplex;
}

function command(
  process: SpawnedProcess,
  signal: AbortSignal,
  schedule: ScheduleTimeout,
  input?: Uint8Array,
): Promise<string> {
  return new Promise((resolve, reject) => {
    let output = '';
    let stderr = '';
    let settled = false;
    let cancel = (): void => {};
    let detach = (): void => {};
    const finish = (error?: unknown): void => {
      if (settled) return;
      settled = true;
      cancel();
      detach();
      if (error !== undefined) {
        void retireProcess(process, schedule).outcome;
        reject(error);
      } else resolve(output);
    };
    process.stdout.setEncoding('utf8');
    process.stderr.setEncoding('utf8');
    process.stdout.on('data', (chunk: string) => {
      output += chunk;
      if (output.length > 8192) finish(new Error('gateway command output exceeds 8192 bytes'));
    });
    process.stderr.on('data', (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-4096);
    });
    process.stdin.on('error', finish);
    void process.exit.then(
      code =>
        finish(code === 0 ? undefined : new Error(`gateway command failed (${code}): ${stderr}`)),
      finish,
    );
    const registration = registerAbort(signal, () =>
      finish(signal.reason ?? new Error('cancelled')),
    );
    detach = registration.detach;
    if (registration.aborted) finish(new Error('cancelled'));
    if (!settled) {
      cancel = schedule(() => finish(new Error('gateway command timed out')), 30_000);
      process.stdin.end(input);
    }
  });
}
