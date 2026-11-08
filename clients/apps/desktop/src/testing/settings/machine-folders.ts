/**
 * The Repositories folders of the machines as the main process keeps them, in memory: the folder
 * half of `MachineActions` for a test. The main process refuses a bad folder; the window never sends
 * one (the state checks it first), so this keeps what it is given.
 */
import * as Effect from 'effect/Effect';
import type {MachineActions} from '../../core/index.js';

export interface MemoryFolders {
  readonly actions: Pick<MachineActions, 'repositories' | 'setRepositories'>;
  /** The folders kept now, by host key. */
  readonly kept: () => Readonly<Record<string, string>>;
}

export function memoryFolders(initial: Readonly<Record<string, string>> = {}): MemoryFolders {
  let kept: Record<string, string> = {...initial};
  return {
    actions: {
      repositories: Effect.sync(() => ({...kept})),
      setRepositories: (host, directory) =>
        Effect.sync(() => {
          const {[host]: _gone, ...rest} = kept;
          kept = directory === '' ? rest : {...rest, [host]: directory};
          return {kind: 'saved', directories: {...kept}} as const;
        }),
    },
    kept: () => ({...kept}),
  };
}
