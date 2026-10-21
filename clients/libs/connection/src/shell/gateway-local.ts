/** Local startup uses the same gateway executable, then HTTP/2 dials the Unix socket directly. */

import {randomUUID} from 'node:crypto';
import {watch} from 'node:fs';
import {chmod, mkdir, rename, rm, writeFile} from 'node:fs/promises';
import {connect, type Socket} from 'node:net';
import {homedir} from 'node:os';
import {join} from 'node:path';
import type {Duplex} from 'node:stream';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {GATEWAY_DIRECTORY, type GatewayTarget, gatewayArtifact} from './gateway-provision.js';
import {nodeSpawn} from './link.js';
import {retireProcess} from './process.js';

export async function localGateway(
  resources: string,
  target: GatewayTarget,
  signal: AbortSignal,
  schedule: ScheduleTimeout,
): Promise<Duplex> {
  const directory = join(homedir(), GATEWAY_DIRECTORY);
  await mkdir(directory, {recursive: true, mode: 0o700});
  // Read first to fail clearly when development skipped the Go build.
  const bytes = await gatewayArtifact(resources, target);
  const binary = join(directory, 'vibesysd');
  const temporary = join(directory, `.vibesysd-${randomUUID()}`);
  // Atomic rename also permits replacing a binary an older serving process still has open.
  try {
    await writeFile(temporary, bytes, {flag: 'wx', mode: 0o700});
    await chmod(temporary, 0o700);
    await rename(temporary, binary);
  } finally {
    await rm(temporary, {force: true});
  }
  return dialLocalGateway(join(directory, 'sock'), binary, signal, schedule);
}

function dialLocalGateway(
  socket: string,
  binary: string,
  signal: AbortSignal,
  schedule: ScheduleTimeout,
): Promise<Duplex> {
  return new Promise((resolve, reject) => {
    let done = false;
    let dialing = false;
    let pending = false;
    let active: Socket | null = null;
    let started = false;
    let cancel = (): void => {};
    let detach = (): void => {};
    const watcher = watch(join(socket, '..'), () => {
      pending = true;
      dial();
    });
    let child: ReturnType<typeof nodeSpawn> | null = null;
    const finish = (error?: unknown, duplex?: Duplex): void => {
      if (done) {
        duplex?.destroy();
        return;
      }
      done = true;
      if (active !== duplex) active?.destroy();
      watcher.close();
      cancel();
      detach();
      if (child !== null) void retireProcess(child, schedule).outcome;
      if (error !== undefined) reject(error);
      else if (duplex !== undefined) resolve(duplex);
    };
    const dial = (): void => {
      if (done || dialing) return;
      dialing = true;
      pending = false;
      const connection = connect(socket);
      active = connection;
      connection.once('connect', () => finish(undefined, connection));
      connection.once('error', (error: NodeJS.ErrnoException) => {
        dialing = false;
        connection.destroy();
        if (error.code !== 'ENOENT' && error.code !== 'ECONNREFUSED') {
          finish(error);
          return;
        }
        if (!started) {
          started = true;
          child = nodeSpawn(binary, ['connect'], {});
          child.stdout.resume();
          child.stderr.resume();
          void child.exit.then(code => {
            if (!done) finish(new Error(`gateway startup exited: ${code}`));
          });
        }
        if (pending) dial();
      });
    };
    watcher.on('error', finish);
    const registration = registerAbort(signal, () =>
      finish(signal.reason ?? new Error('cancelled')),
    );
    detach = registration.detach;
    if (registration.aborted) finish(new Error('cancelled'));
    if (!done) {
      cancel = schedule(() => finish(new Error('gateway startup timed out')), 15_000);
      dial();
    }
  });
}
