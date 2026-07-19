#!/usr/bin/env node

import {type ChildProcess, spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {accessSync, closeSync, constants, openSync, realpathSync} from 'node:fs';
import {access, mkdtemp, readFile, rm} from 'node:fs/promises';
import {createConnection} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const READY_TIMEOUT_MS = 30_000;
const SHUTDOWN_TIMEOUT_MS = 10_000;
const BACKEND_EXIT_GRACE_MS = 2_000;

export async function launch(argv: string[]): Promise<number> {
  if (argv.some(argument => argument === '-h' || argument === '--help')) {
    const backend = resolveBackendCommand();
    if (!backend) return reportMissingPython();
    return runToCompletion(backend.command, [...backend.args, ...argv, '--headless']);
  }

  const backend = resolveBackendCommand();
  if (!backend) return reportMissingPython();

  const runtime = process.env['VIBESYS_TUI_RUNTIME'] ?? 'bun';
  if (!(await executableExists(runtime))) {
    console.error('vibesys-tui: Bun is required by the OpenTUI client.');
    return 1;
  }

  const entrypoint =
    process.env['VIBESYS_TUI_ENTRYPOINT'] ??
    join(dirname(fileURLToPath(import.meta.url)), 'index.js');
  if (!(await fileExists(entrypoint))) {
    console.error('vibesys-tui: TUI build is missing; run `pnpm --dir clients/tui build`.');
    return 1;
  }

  const sessionDir = await mkdtemp(join(tmpdir(), 'vibesys-session-'));
  const socketPath = join(sessionDir, 'control.sock');
  const backendLogPath = join(sessionDir, 'backend.log');
  const backendLogFd = openSync(backendLogPath, 'w');
  let backendLogClosed = false;
  const backendProcess = spawn(
    backend.command,
    [...backend.args, ...argv, '--headless', '--control-socket', socketPath],
    {
      detached: true,
      stdio: ['ignore', backendLogFd, backendLogFd],
    },
  );

  let frontend: ChildProcess | undefined;
  const cleanup = async () => {
    if (frontend && frontend.exitCode === null && frontend.signalCode === null) {
      frontend.kill('SIGTERM');
      await waitOrKill(frontend);
    }
    await terminateBackend(backendProcess);
    if (!backendLogClosed) {
      backendLogClosed = true;
      closeSync(backendLogFd);
    }
    await rm(sessionDir, {recursive: true, force: true});
  };
  let cleanupStarted: Promise<void> | undefined;
  const runCleanup = () => {
    cleanupStarted ??= cleanup();
    return cleanupStarted;
  };
  const disposeSignalCleanup = installSignalCleanup(runCleanup);
  try {
    if (!(await waitUntilReady(socketPath, backendProcess))) {
      await reportBackendFailure(backendProcess, backendLogPath);
      return backendProcess.exitCode ?? 1;
    }
    frontend = spawn(runtime, [entrypoint], {
      env: {...process.env, VIBESYS_CONTROL_SOCKET: socketPath},
      stdio: 'inherit',
    });
    return await monitor(frontend, backendProcess);
  } finally {
    disposeSignalCleanup();
    await runCleanup();
  }
}

interface BackendCommand {
  command: string;
  args: string[];
}

function resolveBackendCommand(): BackendCommand | undefined {
  const configuredPython = process.env['VIBESYS_PYTHON'];
  if (configuredPython) return {command: configuredPython, args: ['-m', 'vibesys.cli']};
  if (commandExistsSync('python3')) return {command: 'python3', args: ['-m', 'vibesys.cli']};
  if (commandExistsSync('python')) return {command: 'python', args: ['-m', 'vibesys.cli']};
  return undefined;
}

function reportMissingPython(): number {
  console.error(
    'vibesys-tui: Python is required and must have the vibesys package installed. Set VIBESYS_PYTHON to the Python executable to use.',
  );
  return 1;
}

function installSignalCleanup(cleanup: () => Promise<void>): () => void {
  let cleanupStarted: Promise<void> | undefined;
  const runCleanup = () => {
    cleanupStarted ??= cleanup();
    return cleanupStarted;
  };
  const onSignal = (signal: NodeJS.Signals) => {
    runCleanup().finally(() => process.exit(signalExitCode(signal)));
  };
  process.once('SIGINT', onSignal);
  process.once('SIGTERM', onSignal);
  process.once('SIGHUP', onSignal);
  return () => {
    process.off('SIGINT', onSignal);
    process.off('SIGTERM', onSignal);
    process.off('SIGHUP', onSignal);
  };
}

async function waitUntilReady(socketPath: string, backend: ChildProcess): Promise<boolean> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (backend.exitCode !== null || backend.signalCode !== null) return false;
    try {
      const response = await querySnapshot(socketPath);
      if (response['ok'] === true) return true;
    } catch {
      await sleep(50);
    }
  }
  return false;
}

function querySnapshot(socketPath: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const socket = createConnection(socketPath);
    let buffer = '';
    const fail = (error: Error) => {
      socket.destroy();
      reject(error);
    };
    socket.setEncoding('utf8');
    socket.setTimeout(500, () => fail(new Error('Readiness probe timed out')));
    socket.once('error', fail);
    socket.once('connect', () => {
      socket.write(
        `${JSON.stringify({
          protocol_version: 1,
          request_id: randomUUID(),
          timestamp: '1970-01-01T00:00:00Z',
          type: 'query.snapshot',
        })}\n`,
      );
    });
    socket.on('data', chunk => {
      buffer += chunk.toString();
      const newline = buffer.indexOf('\n');
      if (newline === -1) return;
      const line = buffer.slice(0, newline);
      socket.end();
      try {
        resolve(JSON.parse(line) as Record<string, unknown>);
      } catch (error) {
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
    socket.once('close', hadError => {
      if (hadError) return;
      if (!buffer.includes('\n')) reject(new Error('Backend closed before readiness response'));
    });
  });
}

async function monitor(frontend: ChildProcess, backend: ChildProcess): Promise<number> {
  while (true) {
    const frontendCode = exitStatus(frontend);
    const backendCode = exitStatus(backend);
    if (frontendCode !== undefined) {
      if (backendCode === undefined) {
        const gracefulBackendCode = await waitForExit(backend, BACKEND_EXIT_GRACE_MS);
        if (gracefulBackendCode === undefined) {
          await terminateBackend(backend);
          return frontendCode === 0 ? 0 : normalizeFrontendExit(frontendCode);
        }
        return frontendCode === 0 ? gracefulBackendCode : normalizeFrontendExit(frontendCode);
      }
      return frontendCode === 0 ? backendCode : normalizeFrontendExit(frontendCode);
    }
    if (backendCode !== undefined) {
      const finalFrontendCode = await waitForExit(frontend);
      return finalFrontendCode ?? backendCode;
    }
    await sleep(50);
  }
}

async function terminateBackend(backend: ChildProcess): Promise<void> {
  if (exitStatus(backend) !== undefined) return;
  if (backend.pid === undefined) return;
  try {
    process.kill(-backend.pid, 'SIGTERM');
  } catch {
    return;
  }
  if ((await waitForExit(backend, SHUTDOWN_TIMEOUT_MS)) !== undefined) return;
  try {
    process.kill(-backend.pid, 'SIGKILL');
  } catch {
    return;
  }
  await waitForExit(backend);
}

async function waitOrKill(process: ChildProcess): Promise<void> {
  if ((await waitForExit(process, SHUTDOWN_TIMEOUT_MS)) !== undefined) return;
  process.kill('SIGKILL');
  await waitForExit(process);
}

async function reportBackendFailure(backend: ChildProcess, logPath: string): Promise<void> {
  const code = exitStatus(backend) ?? 1;
  console.error(`vibesys-tui: backend exited with status ${code}`);
  const tail = await readLogTail(logPath);
  if (tail.length > 0) console.error(tail.join('\n'));
}

async function readLogTail(path: string): Promise<string[]> {
  try {
    return (await readFile(path, 'utf8')).split(/\r?\n/).slice(-20);
  } catch {
    return [];
  }
}

function runToCompletion(command: string, args: string[]): Promise<number> {
  return new Promise(resolve => {
    const child = spawn(command, args, {stdio: 'inherit'});
    child.once('exit', (code, signal) => resolve(code ?? signalExitCode(signal)));
    child.once('error', error => {
      console.error(`vibesys-tui: failed to start backend: ${error.message}`);
      resolve(1);
    });
  });
}

function waitForExit(process: ChildProcess, timeoutMs?: number): Promise<number | undefined> {
  const status = exitStatus(process);
  if (status !== undefined) return Promise.resolve(status);
  return new Promise(resolve => {
    let timeout: NodeJS.Timeout | undefined;
    const done = (code: number | null, signal: NodeJS.Signals | null) => {
      if (timeout) clearTimeout(timeout);
      resolve(code ?? signalExitCode(signal));
    };
    process.once('exit', done);
    if (timeoutMs !== undefined) {
      timeout = setTimeout(() => {
        process.off('exit', done);
        resolve(undefined);
      }, timeoutMs);
    }
  });
}

function exitStatus(process: ChildProcess): number | undefined {
  if (process.exitCode !== null) return process.exitCode;
  if (process.signalCode !== null) return signalExitCode(process.signalCode);
  return undefined;
}

function signalExitCode(signal: NodeJS.Signals | null): number {
  if (signal === 'SIGHUP') return 129;
  if (signal === 'SIGINT') return 130;
  if (signal === 'SIGTERM') return 143;
  if (!signal) return 1;
  return 1;
}

function normalizeFrontendExit(code: number): number {
  return code === 130 ? 130 : code;
}

function commandExistsSync(command: string): boolean {
  if (command.includes('/')) return executableExistsSync(command);
  for (const path of (process.env['PATH'] ?? '').split(':')) {
    if (executableExistsSync(join(path, command))) return true;
  }
  return false;
}

async function executableExists(command: string): Promise<boolean> {
  if (command.includes('/')) return fileIsExecutable(command);
  for (const path of (process.env['PATH'] ?? '').split(':')) {
    if (await fileIsExecutable(join(path, command))) return true;
  }
  return false;
}

function executableExistsSync(path: string): boolean {
  try {
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

async function fileExists(path: string): Promise<boolean> {
  return access(path, constants.F_OK)
    .then(() => true)
    .catch(() => false);
}

async function fileIsExecutable(path: string): Promise<boolean> {
  return access(path, constants.X_OK)
    .then(() => true)
    .catch(() => false);
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function isMainModule(): boolean {
  if (process.argv[1] === undefined) return false;
  try {
    return realpathSync(fileURLToPath(import.meta.url)) === realpathSync(process.argv[1]);
  } catch {
    return false;
  }
}

if (isMainModule()) {
  launch(process.argv.slice(2)).then(code => {
    process.exitCode = code;
  });
}
