/**
 * One machine's VibeSys, in memory: the `vibesys` command line as the hosts drive it, and the Unix
 * sockets its detached servers listen on. The fakes of `LocalHost`'s system and of `SshHost`'s ssh
 * client both translate their own argv into calls on a node, so both hosts face the same machine.
 *
 * `vibesys --detach ARGS` asks the world's server script what to run, listens on a socket under the
 * registry root, and prints the record; `vibesys instances stop ID --json` stops it. Every other
 * command line is answered by the world's command script.
 */
import type {CommandResult, ServerScript} from './fake-host.js';
import {FakeNetwork} from './fake-network.js';
import {fakeRecord} from './fake-record.js';

export interface FakeNodeScripts {
  server: (args: readonly string[]) => ServerScript;
  command: (argv: readonly string[]) => CommandResult;
  /**
   * A `DetachedLaunchFailure` document `vibesys --detach ARGS` prints instead of starting anything
   * (it exits with the document's `exit_code`), or null to start a server.
   */
  launchFailure?: (args: readonly string[], node: FakeVibesysNode) => {exit_code: number} | null;
}

export class FakeVibesysNode {
  readonly network = new FakeNetwork();
  /** The working directory of each `--detach` start, in order. */
  readonly startDirectories: (string | undefined)[] = [];
  readonly #scripts: FakeNodeScripts;
  readonly #live = new Map<string, string>();
  #next = 0;

  constructor(scripts: FakeNodeScripts) {
    this.#scripts = scripts;
  }

  run(argv: readonly string[], cwd: string | undefined): CommandResult {
    if (argv[0] === '--detach') return this.#detach(argv.slice(1), cwd);
    if (argv[0] === 'instances' && argv[1] === 'stop' && argv[3] === '--json') {
      const id = argv[2] ?? '';
      const socketPath = this.#live.get(id);
      if (socketPath === undefined) {
        return {
          code: 1,
          stdout: `{"version":1,"id":"${id}","outcome":"not_running"}\n`,
          stderr: '',
        };
      }
      this.#live.delete(id);
      this.network.unlisten(socketPath);
      return {code: 0, stdout: `{"version":1,"id":"${id}","outcome":"stopped"}\n`, stderr: ''};
    }
    return this.#scripts.command(argv);
  }

  /** Stop every live server at once, as if the machine rebooted. */
  stopAll(): void {
    for (const socketPath of this.#live.values()) this.network.unlisten(socketPath);
    this.#live.clear();
  }

  /** The live servers' ids and socket paths. */
  get live(): ReadonlyMap<string, string> {
    return this.#live;
  }

  #detach(args: readonly string[], cwd: string | undefined): CommandResult {
    this.startDirectories.push(cwd);
    const failure = this.#scripts.launchFailure?.(args, this) ?? null;
    if (failure !== null) {
      return {code: failure.exit_code, stdout: `${JSON.stringify(failure)}\n`, stderr: ''};
    }
    const script = this.#scripts.server(args);
    if (typeof script !== 'function') {
      return {code: 1, stdout: '', stderr: `${script.logTail}\n`};
    }
    this.#next += 1;
    const id = this.#next.toString(16).padStart(12, '0');
    const socketPath = `/run/user/1000/vibesys/runs/${id}/control.sock`;
    this.network.listen(socketPath, script);
    this.#live.set(id, socketPath);
    return {code: 0, stdout: `${JSON.stringify(fakeRecord(id, socketPath))}\n`, stderr: ''};
  }
}
