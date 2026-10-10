/**
 * A `LocalSystem` with in-memory processes and sockets, so `LocalHost` runs the Host contract
 * without starting Python. Spawned commands are recognized by the module they run, exactly as
 * `LocalHost` builds them, and answered from the world's scripts; sockets are a `FakeNetwork`.
 */
import {PassThrough} from 'node:stream';
import type {CommandResult, ServerScript} from '../fake-host.js';
import {FakeNetwork} from '../fake-network.js';
import type {LocalSystem, ProcessLike} from '../local-host.js';

export interface FakeLocalScripts {
  server: (args: readonly string[]) => ServerScript;
  command: (argv: readonly string[]) => CommandResult;
}

/** A process whose output is `stdout`/`stderr` and which exits once both are read and `end` runs. */
function fakeProcess(onKill: () => void): {
  process: ProcessLike;
  end: (code: number | null, stdout?: string, stderr?: string) => void;
} {
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  let resolveCode: (code: number | null) => void = () => {};
  const code = new Promise<number | null>(resolve => {
    resolveCode = resolve;
  });
  const drained = (stream: PassThrough): Promise<void> =>
    new Promise(resolve => stream.once('end', () => resolve()));
  // Like a real child's 'close': the exit is reported after its output has been read.
  const exit = Promise.all([drained(stdout), drained(stderr), code]).then(([, , value]) => value);
  let ended = false;
  const end = (value: number | null, out = '', err = ''): void => {
    if (ended) return;
    ended = true;
    stdout.end(out);
    stderr.end(err);
    resolveCode(value);
  };
  return {
    process: {
      stdout,
      stderr,
      exit,
      kill: () => {
        onKill();
        end(null);
      },
    },
    end,
  };
}

export function fakeLocalSystem(scripts: FakeLocalScripts): LocalSystem {
  const network = new FakeNetwork();
  let sessions = 0;
  return {
    connect: path => network.connect(path),
    spawn: (_command, args) => {
      const module = args.indexOf('-m');
      const name = args[module + 1];
      const rest = args.slice(module + 2);
      if (name === 'entrypoints.server') {
        const flag = rest.indexOf('--control-socket');
        const socketPath = rest[flag + 1];
        if (flag < 0 || socketPath === undefined)
          throw new Error('server spawned without a socket');
        const script = scripts.server(rest.slice(0, flag));
        const {process, end} = fakeProcess(() => network.unlisten(socketPath));
        if (typeof script === 'function') network.listen(socketPath, script);
        else end(script.code, '', `${script.logTail}\n`);
        return process;
      }
      if (name === 'entrypoints.launcher') {
        const result = scripts.command(rest);
        const {process, end} = fakeProcess(() => {});
        end(result.code, result.stdout, result.stderr);
        return process;
      }
      throw new Error(`unexpected command: ${args.join(' ')}`);
    },
    makeSessionDirectory: async () => {
      sessions += 1;
      return `/fake/session-${sessions}`;
    },
    removeDirectory: async () => {},
    // No case waits on time: a scheduled callback never runs, and cancelling it is a no-op.
    scheduleTimeout: () => () => {},
  };
}
