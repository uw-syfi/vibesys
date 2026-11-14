import {ToastStack} from '@vibesys/ui';
import type * as Effect from 'effect/Effect';
import {type JSX, type ReactNode, useState} from 'react';
import type {MachineActions, MachineUndo} from '../../../core/index.js';
import {
  type AddMachineEvent,
  type AddMachineState,
  addMachineStep,
  emptyAddMachine,
  hostFromKey,
  hostLabel,
} from '../../../state/index.js';

/** Runs `effect` and hands its answer to `then`; the entry owns where it runs. */
export type MachineDispatch = <A>(effect: Effect.Effect<A>, then: (value: A) => void) => void;

export interface AddMachineController {
  readonly state: AddMachineState;
  readonly type: (input: string) => void;
  readonly pick: (alias: string) => void;
  readonly submit: (input: string, added?: (host: string) => void) => void;
  readonly reset: () => void;
}

/** What the Machines pages show of machine actions: the dedicated add form, remove, and undo. */
export interface MachineControls {
  readonly add: AddMachineController;
  /** Removes the machine of this host key, offering to put it back. */
  readonly remove: (key: string) => void;
  /** The toasts a removal leaves. */
  readonly toasts: ReactNode;
  /** The row of the machine just added or found already saved, or null. */
  readonly selected: string | null;
  readonly select: (key: string) => void;
}

interface Removed {
  readonly id: string;
  readonly host: string;
  readonly undo: MachineUndo;
}

/**
 * The state of the machines controls over `actions`: what the add form holds, and the removals that
 * can still be undone; none without actions. Every action runs through `dispatch`; a row appears or
 * goes with the host's session, which the actions open and end.
 */
export function useMachineControls(
  actions: MachineActions | undefined,
  dispatch: MachineDispatch,
): MachineControls | undefined {
  const [add, setAdd] = useState<AddMachineState>(emptyAddMachine);
  const [selected, select] = useState<string | null>(null);
  const [removed, setRemoved] = useState<readonly Removed[]>([]);
  const step = (event: AddMachineEvent): void => setAdd(state => addMachineStep(state, event));
  const reset = (): void => setAdd(emptyAddMachine);
  if (actions === undefined) return undefined;
  const submitText = (input: string, added?: (host: string) => void): void => {
    if (add.busy || input.trim() === '') return;
    step({type: 'submitted'});
    dispatch(actions.add(input), outcome => {
      if (outcome.kind === 'refused') step({type: 'refused', reason: outcome.reason});
      else {
        // A host already saved is selected, not saved twice.
        select(outcome.host);
        reset();
        added?.(outcome.host);
      }
    });
  };
  const type = (input: string): void => {
    step({type: 'typed', input});
    dispatch(actions.match(input), aliases => step({type: 'matched', query: input, aliases}));
  };
  const remove = (key: string): void => {
    dispatch(actions.remove(key), outcome => {
      if (outcome.kind === 'removed') {
        setRemoved(list => [{id: `${key}#${list.length}`, host: key, undo: outcome.undo}, ...list]);
      }
    });
  };
  const drop = (id: string): void => setRemoved(list => list.filter(item => item.id !== id));
  const toasts: JSX.Element = (
    <ToastStack
      label="Machines"
      toasts={removed.map(item => ({
        id: item.id,
        tone: 'neutral',
        title: `Removed ${hostLabel(hostFromKey(item.host))}`,
        open: false,
        action: 'Undo',
      }))}
      toggle={() => undefined}
      dismiss={drop}
      act={id => {
        const item = removed.find(candidate => candidate.id === id);
        if (item === undefined) return;
        drop(id);
        dispatch(actions.undo(item.host, item.undo), () => undefined);
      }}
    />
  );
  return {
    add: {
      state: add,
      type,
      pick: alias => submitText(alias),
      submit: submitText,
      reset,
    },
    remove,
    toasts,
    selected,
    select,
  };
}
