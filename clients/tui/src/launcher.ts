#!/usr/bin/env node

import {type ChildProcess, spawn} from 'node:child_process';
import {accessSync, closeSync, constants, openSync, realpathSync} from 'node:fs';
import {access, mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {isThemeName, THEME_NAMES} from './ui/theme.js';

const READY_TIMEOUT_MS = 30_000;
const READY_POLL_INTERVAL_MS = 25;
const SHUTDOWN_TIMEOUT_MS = 10_000;
const BACKEND_EXIT_GRACE_MS = 2_000;
const FRONTEND_EXIT_GRACE_MS = 2_000;

export async function launch(argv: string[]): Promise<number> {
  const python = resolvePythonCommand();
  if (!python) return reportMissingPython();

  if (argv[0] === 'validate') {
    return runToCompletion(python.command, [...python.args, '-m', 'entrypoints.headless', ...argv]);
  }

  if (argv.some(argument => argument === '-h' || argument === '--help')) {
    return runToCompletion(python.command, [...python.args, '-m', 'entrypoints.headless', ...argv]);
  }

  const runtime = process.env['VIBESYS_TUI_RUNTIME'] ?? 'bun';
  if (!(await executableExists(runtime))) {
    console.error('vs: Bun is required by the OpenTUI client.');
    return 1;
  }

  const entrypoint =
    process.env['VIBESYS_TUI_ENTRYPOINT'] ??
    join(dirname(fileURLToPath(import.meta.url)), 'index.js');
  if (!(await fileExists(entrypoint))) {
    console.error('vs: TUI build is missing; run `pnpm --dir clients/tui build`.');
    return 1;
  }

  const requestedTheme = optionValue(argv, '--theme');
  if (requestedTheme !== undefined && !isThemeName(requestedTheme)) {
    console.error(`vs: unknown --theme ${requestedTheme}. Available: ${THEME_NAMES.join(', ')}.`);
    return 2;
  }

  const sessionDir = await mkdtemp(join(tmpdir(), 'vibesys-session-'));
  const socketPath = join(sessionDir, 'control.sock');
  const backendLogPath = join(sessionDir, 'backend.log');
  const backendLogFd = openSync(backendLogPath, 'w');
  let backendLogClosed = false;
  const backendProcess = spawn(
    python.command,
    [...python.args, '-m', 'entrypoints.server', ...argv, '--control-socket', socketPath],
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
    // The frontend does its own connect retries, so it starts now and pays
    // its interpreter and renderer startup while the backend is still coming
    // up. Startup readiness is still watched here: only the launcher can see
    // a backend that dies before it ever listens, and report its log.
    const startupFailed = watchBackendStartup(socketPath, backendProcess).then(ready => {
      if (!ready && frontend) frontend.kill('SIGTERM');
      return !ready;
    });
    frontend = spawn(runtime, [entrypoint], {
      env: frontendEnvironment(socketPath, requestedTheme),
      stdio: 'inherit',
    });
    const exitCode = await monitor(
      frontend,
      backendProcess,
      Boolean(process.env['VIBESYS_RELEASE_SMOKE_MARKER']),
    );
    if (await startupFailed) {
      await reportBackendFailure(backendProcess, backendLogPath);
      return exitStatus(backendProcess) ?? 1;
    }
    return exitCode;
  } finally {
    disposeSignalCleanup();
    await runCleanup();
  }
}

interface PythonCommand {
  command: string;
  args: string[];
}

/**
 * Build the frontend's environment.
 *
 * ``--theme`` is the only theme the launcher resolves. Without it the theme
 * comes from the backend's configuration over the control channel, so any
 * inherited ``VIBESYS_THEME`` is cleared rather than silently overriding it.
 */
function frontendEnvironment(
  socketPath: string,
  requestedTheme: string | undefined,
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {...process.env, VIBESYS_CONTROL_SOCKET: socketPath};
  if (requestedTheme === undefined) delete env['VIBESYS_THEME'];
  else env['VIBESYS_THEME'] = requestedTheme;
  return env;
}

function optionValue(argv: string[], option: string): string | undefined {
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === option) return argv[index + 1];
    if (argument?.startsWith(`${option}=`)) return argument.slice(option.length + 1);
  }
  return undefined;
}

function resolvePythonCommand(): PythonCommand | undefined {
  const configuredPython = process.env['VIBESYS_PYTHON'];
  if (configuredPython) return {command: configuredPython, args: []};
  if (commandExistsSync('python3')) return {command: 'python3', args: []};
  if (commandExistsSync('python')) return {command: 'python', args: []};
  return undefined;
}

function reportMissingPython(): number {
  console.error(
    'vs: Python is required and must have the vibesys package installed. Set VIBESYS_PYTHON to the Python executable to use.',
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

/**
 * Resolve once the backend's control socket exists, false if it never does.
 *
 * The frontend owns the protocol handshake. The launcher only needs to know
 * whether the backend got far enough to listen, which distinguishes a failed
 * start (report the backend log) from a run that failed later (the frontend
 * has already shown the diagnostic).
 */
async function watchBackendStartup(socketPath: string, backend: ChildProcess): Promise<boolean> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await fileExists(socketPath)) return true;
    if (exitStatus(backend) !== undefined) return fileExists(socketPath);
    await sleep(READY_POLL_INTERVAL_MS);
  }
  return false;
}

// biome-ignore lint/complexity/noExcessiveCognitiveComplexity: pre-existing; tracked: #288
async function monitor(
  frontend: ChildProcess,
  backend: ChildProcess,
  releaseSmokeMode: boolean,
): Promise<number> {
  while (true) {
    const frontendCode = exitStatus(frontend);
    const backendCode = exitStatus(backend);
    if (frontendCode !== undefined) {
      if (releaseSmokeMode && frontendCode === 0) return 0;
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
      // The backend never legitimately exits while the frontend is attached
      // (it stays alive for post-run chat until the frontend ends the
      // session), so this is always an unexpected backend death. Give the
      // frontend a moment to exit on its own, then terminate it so the
      // launcher's cleanup can run.
      //
      // This branch's safety depends on the backend never tearing down
      // while a subscription is active or about to redial. Today
      // `_client_disconnected` in `unix_jsonl.py` latches on the first
      // mid-run drop and never unsticks on reconnect, so the backend can
      // exit under a live client, violating that contract. Fixed by the
      // reconnect-aware SubscriptionTracker in #588 (PR #602).
      const gracefulFrontendCode = await waitForExit(frontend, FRONTEND_EXIT_GRACE_MS);
      if (gracefulFrontendCode === undefined) {
        frontend.kill('SIGTERM');
        await waitOrKill(frontend);
        return backendCode;
      }
      return gracefulFrontendCode === 0 ? backendCode : normalizeFrontendExit(gracefulFrontendCode);
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
  console.error(`vs: backend exited with status ${code}`);
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
      console.error(`vs: failed to start backend: ${error.message}`);
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
