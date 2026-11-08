/**
 * What the Machines page does with machines, over the `Machines` role and the window's `Roster`: a
 * save also opens the host's session (so its row appears and it connects), a removal also ends it,
 * and a failure is an outcome the screen shows next to the input, never a defect.
 */
import * as Effect from 'effect/Effect';
import type {MachinesApi} from './machines.js';
import type {MachineUndo} from './machines-codec.js';
import type {Roster} from './roster.js';

export type AddOutcome =
  | {readonly kind: 'added'; readonly host: string; readonly created: boolean}
  | {readonly kind: 'refused'; readonly reason: string};

export type RemoveOutcome =
  | {readonly kind: 'removed'; readonly host: string; readonly undo: MachineUndo}
  | {readonly kind: 'refused'; readonly reason: string};

export type FoldersOutcome =
  | {readonly kind: 'saved'; readonly directories: Readonly<Record<string, string>>}
  | {readonly kind: 'refused'; readonly reason: string};

export interface MachineActions {
  /** The repositories folder kept for each machine, by host key; none when they cannot be read. */
  readonly repositories: Effect.Effect<Readonly<Record<string, string>>>;
  /** Keeps (empty clears) one machine's repositories folder. */
  readonly setRepositories: (host: string, directory: string) => Effect.Effect<FoldersOutcome>;
  /** The aliases to offer for what was typed; none when they cannot be read. */
  readonly match: (query: string) => Effect.Effect<readonly string[]>;
  /** Saves an alias or pasted ssh command and connects to it; a host already saved is found, not saved twice. */
  readonly add: (input: string) => Effect.Effect<AddOutcome>;
  /** Forgets a host and ends its session. */
  readonly remove: (host: string) => Effect.Effect<RemoveOutcome>;
  /** Puts back a removed host and connects to it again. */
  readonly undo: (host: string, undo: MachineUndo) => Effect.Effect<AddOutcome>;
}

export function makeMachineActions(machines: MachinesApi, roster: Roster): MachineActions {
  const refused = (error: {readonly detail: string}) =>
    Effect.succeed({kind: 'refused', reason: error.detail} as const);
  return {
    repositories: machines.repositories.pipe(
      Effect.catchTag('MachinesError', () => Effect.succeed({})),
    ),
    setRepositories: (host, directory) =>
      machines.setRepositories(host, directory).pipe(
        Effect.map(directories => ({kind: 'saved', directories}) as const),
        Effect.catchTag('MachinesError', refused),
      ),
    match: query =>
      machines.match(query).pipe(Effect.catchTag('MachinesError', () => Effect.succeed([]))),
    add: input =>
      machines.add(input).pipe(
        Effect.tap(added => roster.add(added.host)),
        Effect.map(added => ({kind: 'added', host: added.host, created: added.created}) as const),
        Effect.catchTag('MachinesError', refused),
      ),
    remove: host =>
      machines.remove(host).pipe(
        Effect.tap(() => roster.remove(host)),
        Effect.map(undo => ({kind: 'removed', host, undo}) as const),
        Effect.catchTag('MachinesError', refused),
      ),
    undo: (host, undo) =>
      machines.restore(undo).pipe(
        Effect.tap(() => roster.add(host)),
        Effect.map(added => ({kind: 'added', host: added.host, created: added.created}) as const),
        Effect.catchTag('MachinesError', refused),
      ),
  };
}
