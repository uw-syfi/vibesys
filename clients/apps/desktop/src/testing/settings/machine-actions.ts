/**
 * `MachineActions` in memory for composed desktop tests and the browser fixture. Inputs go through
 * the same closed SSH parser as production; successful adds, removes and restores update the saved
 * roster, while repository folders use the same faithful in-memory store as the settings tests.
 */
import {create} from '@bufbuild/protobuf';
import {
  DesktopHostSettingsSchema,
  DesktopMachineRestoreSchema,
  DesktopSshOptionsSchema,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import type {MachineActions, MachineUndo} from '../../core/index.js';
import {matchAliases, SshCommandError, targetFromInput} from '../../state/index.js';
import {memoryFolders} from './machine-folders.js';

export interface MemoryMachineActions {
  readonly actions: MachineActions;
  /** The saved host keys now, in insertion order. */
  readonly saved: () => readonly string[];
}

function undoOf(input: string): MachineUndo {
  const target = targetFromInput(input);
  const host = `ssh:${target.destination}`;
  const hasOptions =
    target.port !== undefined || target.jump !== undefined || target.identityFile !== undefined;
  return create(DesktopMachineRestoreSchema, {
    host,
    settings: create(DesktopHostSettingsSchema, {
      ...(hasOptions
        ? {
            ssh: create(DesktopSshOptionsSchema, {
              port: target.port,
              jump: target.jump ?? '',
              identity: target.identityFile ?? '',
            }),
          }
        : {}),
    }),
  });
}

/** A deterministic, validating implementation of the full `MachineActions` interface. */
export function memoryMachineActions(
  options: {
    readonly aliases?: readonly string[];
    readonly saved?: readonly string[];
    readonly repositories?: Readonly<Record<string, string>>;
  } = {},
): MemoryMachineActions {
  const folders = memoryFolders(options.repositories);
  const saved = new Map<string, MachineUndo>();
  for (const input of options.saved ?? []) {
    const undo = undoOf(input.startsWith('ssh:') ? input.slice(4) : input);
    saved.set(undo.host, undo);
  }
  const actions: MachineActions = {
    ...folders.actions,
    match: query => Effect.succeed(matchAliases(options.aliases ?? [], query)),
    add: input =>
      Effect.sync(() => {
        try {
          const undo = undoOf(input);
          const created = !saved.has(undo.host);
          saved.set(undo.host, undo);
          return {kind: 'added', host: undo.host, created} as const;
        } catch (error) {
          if (error instanceof SshCommandError)
            return {kind: 'refused', reason: error.message} as const;
          throw error;
        }
      }),
    remove: host =>
      Effect.sync(() => {
        const undo = saved.get(host);
        if (undo === undefined)
          return {kind: 'refused', reason: `${host} is not a saved machine.`} as const;
        saved.delete(host);
        return {kind: 'removed', host, undo} as const;
      }),
    undo: (host, undo) =>
      Effect.sync(() => {
        if (undo.host !== host)
          return {kind: 'refused', reason: 'The saved machine does not match this host.'} as const;
        const created = !saved.has(host);
        saved.set(host, undo);
        return {kind: 'added', host, created} as const;
      }),
  };
  return {actions, saved: () => [...saved.keys()]};
}
