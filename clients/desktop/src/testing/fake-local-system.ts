/**
 * A `LocalSystem` over a `FakeVibesysNode`, so `LocalHost` runs the Host contract without starting
 * Python. Spawned commands are recognized by the module they run, exactly as `LocalHost` builds
 * them (`python -m entrypoints.launcher ARGV`), and answered by the node; sockets are its network.
 */
import type {LocalSystem} from '../local-host.js';
import {finishedProcess} from './fake-process.js';
import type {FakeVibesysNode} from './fake-vibesys-node.js';

export function fakeLocalSystem(node: FakeVibesysNode): LocalSystem {
  return {
    connect: path => node.network.connect(path),
    spawn: (_command, args, cwd) => {
      const module = args.indexOf('-m');
      if (module < 0 || args[module + 1] !== 'entrypoints.launcher') {
        throw new Error(`unexpected command: ${args.join(' ')}`);
      }
      const result = node.run(args.slice(module + 2), cwd);
      return finishedProcess(result.code, result.stdout, result.stderr);
    },
  };
}
