import {Button, Row, ToastStack} from '@vibesys/ui';
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
import {AddMachine} from './add-machine.js';

/** Runs `effect` and hands its answer to `then`; the entry owns where it runs. */
export type MachineDispatch = <A>(effect: Effect.Effect<A>, then: (value: A) => void) => void;

/** What the Machines page shows of the machines controls: the Add machine bar, a remove, and the toasts of a removal. */
export interface MachineControls {
  /** The Add machine button, or the open input in its place. */
  readonly bar: ReactNode;
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
 * The state of the machines controls over `actions`: whether the input is open, what it holds, and
 * the removals that can still be undone; none without actions. Every action runs through `dispatch`; a row appears or
 * goes with the host's session, which the actions open and end.
 */
export function useMachineControls(
  actions: MachineActions | undefined,
  dispatch: MachineDispatch,
): MachineControls | undefined {
  const [open, setOpen] = useState(false);
  const [add, setAdd] = useState<AddMachineState>(emptyAddMachine);
  const [selected, select] = useState<string | null>(null);
  const [removed, setRemoved] = useState<readonly Removed[]>([]);
  const step = (event: AddMachineEvent): void => setAdd(state => addMachineStep(state, event));
  const close = (): void => {
    setOpen(false);
    setAdd(emptyAddMachine);
  };
  if (actions === undefined) return undefined;
  const submitText = (input: string): void => {
    if (add.busy || input.trim() === '') return;
    step({type: 'submitted'});
    dispatch(actions.add(input), outcome => {
      if (outcome.kind === 'refused') step({type: 'refused', reason: outcome.reason});
      else {
        // A host already saved is selected, not saved twice.
        select(outcome.host);
        close();
      }
    });
  };
  const type = (input: string): void => {
    step({type: 'typed', input});
    dispatch(actions.match(input), aliases => step({type: 'matched', query: input, aliases}));
  };
  const opened = (): void => {
    setOpen(true);
    dispatch(actions.match(''), aliases => step({type: 'matched', query: '', aliases}));
  };
  const remove = (key: string): void => {
    dispatch(actions.remove(key), outcome => {
      if (outcome.kind === 'removed') {
        setRemoved(list => [{id: `${key}#${list.length}`, host: key, undo: outcome.undo}, ...list]);
      }
    });
  };
  const drop = (id: string): void => setRemoved(list => list.filter(item => item.id !== id));
  const bar: JSX.Element = open ? (
    <AddMachine
      state={add}
      type={type}
      pick={alias => submitText(alias)}
      submit={() => submitText(add.input)}
      close={close}
    />
  ) : (
    <Row justify="start">
      <Button variant="secondary" icon="plus" text="Add machine" press={opened} />
    </Row>
  );
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
  return {bar, remove, toasts, selected, select};
}
