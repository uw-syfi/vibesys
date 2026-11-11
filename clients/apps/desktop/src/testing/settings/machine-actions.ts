/**
 * `MachineActions` for the fixture page: the hosts shown are fixed by its address, so a machine added
 * or removed is refused in words the person reads next to the input, and the aliases offered are
 * generic. The folders half is the memory folders of `machine-folders.ts`.
 */
import * as Effect from 'effect/Effect';
import type {MachineActions} from '../../core/index.js';
import {matchAliases} from '../../state/index.js';
import {memoryFolders} from './machine-folders.js';

const ALIASES = ['host-a', 'host-b', 'host-c', 'cluster-login'];

const REFUSED = 'This page shows fixed machines and cannot connect to others.';

export function fixtureMachineActions(): MachineActions {
  return {
    ...memoryFolders().actions,
    match: query => Effect.succeed(matchAliases(ALIASES, query)),
    add: () => Effect.succeed({kind: 'refused', reason: REFUSED} as const),
    remove: () => Effect.succeed({kind: 'refused', reason: REFUSED} as const),
    undo: () => Effect.succeed({kind: 'refused', reason: REFUSED} as const),
  };
}
