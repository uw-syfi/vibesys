/**
 * The Repositories folder of each machine as the window holds it: read once from the main process,
 * changed by a save the main process accepted, and read again when a machine is added, removed or
 * put back (the main process keeps a folder with its machine). A read that fails leaves what the
 * window holds; a refused save changes nothing and carries the main process's reason.
 */
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {FoldersOutcome, MachineActions} from './actions.js';

export interface MachineFolders {
  /** The folder kept for each machine that has one, by host key. */
  readonly state: Atom.Atom<Readonly<Record<string, string>>>;
  /** Read them from the main process. Never fails. */
  readonly load: Effect.Effect<void>;
  /** Keep (empty clears) one machine's folder; the state follows an accepted save. */
  readonly save: (host: string, directory: string) => Effect.Effect<FoldersOutcome>;
  /** `actions` with every add, remove and undo followed by a read of the folders. */
  readonly follow: (actions: MachineActions) => MachineActions;
}

export const makeMachineFolders = Effect.fn('MachineFolders.make')(function* (
  actions: Pick<MachineActions, 'repositories' | 'setRepositories'>,
) {
  const ref = yield* SubscriptionRef.make<Readonly<Record<string, string>>>({});
  const load = actions.repositories.pipe(Effect.flatMap(held => SubscriptionRef.set(ref, held)));
  const save = (host: string, directory: string): Effect.Effect<FoldersOutcome> =>
    actions
      .setRepositories(host, directory)
      .pipe(
        Effect.tap(outcome =>
          outcome.kind === 'saved' ? SubscriptionRef.set(ref, outcome.directories) : Effect.void,
        ),
      );
  const follow = (inner: MachineActions): MachineActions => ({
    ...inner,
    add: input => Effect.tap(inner.add(input), () => load),
    remove: host => Effect.tap(inner.remove(host), () => load),
    undo: (host, undo) => Effect.tap(inner.undo(host, undo), () => load),
  });
  return {
    state: Atom.keepAlive(Atom.subscriptionRef(ref)),
    load,
    save,
    follow,
  } satisfies MachineFolders;
});
