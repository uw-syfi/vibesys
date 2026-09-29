/**
 * The `vibesys web home` child process: start it, read its capability from stdout, stop it.
 *
 * Stdout is read for the one announcement line and never forwarded: it carries the token.
 * Stderr (method, path and status per request; tracebacks) is forwarded line by line, and its
 * tail explains a failed start or a crash.
 */
import {type ChildProcessByStdio, spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import type {Readable} from 'node:stream';

/** `_announce` in src/entrypoints/web_home/app.py prints exactly this, once. */
const ANNOUNCEMENT = /^VibeSys home: (http:\/\/127\.0\.0\.1:\d{1,5})\/\?token=([\w-]+)$/;
const TAIL_LINES = 40;
const HEALTH_TIMEOUT_MS = 5_000;

export interface HomeAddress {
  readonly origin: string;
  readonly token: string;
}

/** Why the launched process ended: `stop()`, a hand-over to a running home server, or a crash. */
type HomeEnd =
  | {readonly kind: 'stopped'}
  | {readonly kind: 'reused'}
  | {readonly kind: 'crashed'; readonly detail: string};

export interface Home extends HomeAddress {
  /** Settles once the launched process has exited. */
  readonly ended: Promise<HomeEnd>;
  /** SIGINT the launched process group, SIGKILL after the grace; resolves once it exited. */
  stop(): Promise<void>;
}

export interface HomeOptions {
  /** The argv; its first element is the executable. */
  readonly command: readonly [string, ...string[]];
  readonly cwd: string;
  readonly env: NodeJS.ProcessEnv;
  readonly readyTimeoutMs: number;
  readonly stopGraceMs: number;
  readonly onStderr: (line: string) => void;
}

/** A start that failed; the message is a headline, then the server's stderr tail. */
export class HomeStartError extends Error {
  override readonly name = 'HomeStartError';
}

interface Exit {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly error: Error | null;
}

type HomeProcess = ChildProcessByStdio<null, Readable, Readable>;

export function parseAnnouncement(line: string): HomeAddress | null {
  const match = ANNOUNCEMENT.exec(line.trim());
  const origin = match?.[1];
  const token = match?.[2];
  return origin === undefined || token === undefined ? null : {origin, token};
}

export async function startHome(options: HomeOptions): Promise<Home> {
  const child = launch(options);
  const tail: string[] = [];
  createInterface({input: child.stderr}).on('line', line => {
    tail.push(line);
    if (tail.length > TAIL_LINES) tail.shift();
    options.onStderr(line);
  });
  const exit = new Promise<Exit>(resolve => {
    child.once('error', error => resolve({code: null, signal: null, error}));
    child.once('close', (code, signal) => resolve({code, signal, error: null}));
  });
  const signalGroup = (signal: NodeJS.Signals): void => {
    // Only while the leader lives: once it is reaped, its pid and group id can be reused.
    if (child.pid === undefined || child.exitCode !== null || child.signalCode !== null) return;
    try {
      process.kill(-child.pid, signal);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error;
    }
  };
  let address: HomeAddress;
  try {
    address = await announced(child.stdout, exit, options);
    if (!(await answers(address))) {
      throw new HomeStartError(`the home server at ${address.origin} did not answer /health`);
    }
  } catch (error) {
    signalGroup('SIGKILL');
    await exit;
    if (error instanceof HomeStartError) throw new HomeStartError(report(error.message, tail));
    throw error;
  }
  let stopping = false;
  const ended = exit.then(async (end): Promise<HomeEnd> => {
    if (stopping) return {kind: 'stopped'};
    // `vibesys web home` that finds a running home server prints its URL and exits 0.
    if (end.code === 0 && (await answers(address))) return {kind: 'reused'};
    return {kind: 'crashed', detail: report(`the home server ${describe(end)}`, tail)};
  });
  return {
    ...address,
    ended,
    async stop() {
      stopping = true;
      signalGroup('SIGINT');
      const escalate = setTimeout(() => signalGroup('SIGKILL'), options.stopGraceMs);
      await ended;
      clearTimeout(escalate);
    },
  };
}

function launch(options: HomeOptions): HomeProcess {
  const [executable, ...args] = options.command;
  try {
    // detached: the child leads a new process group, so one signal reaches `uv` and Python.
    // Run servers start their own sessions (start_new_session) and outlive it by design.
    return spawn(executable, args, {
      cwd: options.cwd,
      env: options.env,
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    throw new HomeStartError(`cannot run ${executable}: ${(error as Error).message}`);
  }
}

function announced(
  stdout: Readable,
  exit: Promise<Exit>,
  options: HomeOptions,
): Promise<HomeAddress> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      const seconds = options.readyTimeoutMs / 1000;
      reject(new HomeStartError(`the home server was not ready within ${seconds} s`));
    }, options.readyTimeoutMs);
    createInterface({input: stdout}).on('line', line => {
      const address = parseAnnouncement(line);
      if (address === null) return;
      clearTimeout(timer);
      resolve(address);
    });
    void exit.then(end => {
      clearTimeout(timer);
      reject(
        new HomeStartError(
          end.error === null
            ? `the home server ${describe(end)} before it was ready`
            : `cannot run ${options.command[0]}: ${end.error.message}`,
        ),
      );
    });
  });
}

async function answers({origin, token}: HomeAddress): Promise<boolean> {
  try {
    const response = await fetch(`${origin}/health?token=${encodeURIComponent(token)}`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    return response.ok && (await response.text()) === 'vibesys-ok\n';
  } catch {
    // A fetch error can quote the URL, which carries the token; callers report the origin.
    return false;
  }
}

function describe(end: Exit): string {
  return end.signal === null
    ? `exited with code ${end.code ?? 'unknown'}`
    : `was killed by ${end.signal}`;
}

function report(headline: string, tail: readonly string[]): string {
  return [headline, ...tail].join('\n');
}
