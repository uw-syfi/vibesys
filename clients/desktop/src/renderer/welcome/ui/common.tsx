/**
 * What the welcome view's presentational components share: where their events go, the stop
 * controls, and the path field. Components render the state they are given and report events; they perform no I/O and
 * hold no state that changes behavior.
 */

import type {HostKey, StopView} from '@vibesys/entities';
import {Button, Combobox} from '@vibesys/ui';
import type {InputHTMLAttributes, JSX} from 'react';
import {
  type CompletionAction,
  type CompletionField,
  type CompletionState,
  suggestions,
} from '../state/completion.js';
import type {HostAction} from '../state/host.js';
import type {PickerAction} from '../state/picker.js';
import type {StartAction} from '../state/start.js';
import type {RunCommand, WelcomeAction} from '../state/welcome.js';

/** Where the view's events go: one dispatcher per screen store. */
export interface WelcomeSenders {
  readonly welcome: (action: WelcomeAction) => void;
  readonly host: (action: HostAction) => void;
  readonly start: (action: StartAction) => void;
  readonly picker: (action: PickerAction) => void;
  readonly completion: (action: CompletionAction) => void;
}

export type Dispatch = (command: RunCommand) => void;

/** Send `command` (attach, resume, stop, ...) to the welcome store. */
export function commands(send: WelcomeSenders): Dispatch {
  return command => send.welcome({type: 'command', command});
}

/** A live run's stop controls: Stop, "Stopping…", or the failure with Force stop. */
export function StopControls(props: {
  readonly view: StopView | undefined;
  readonly stop: (force: boolean) => void;
}): JSX.Element {
  const view = props.view;
  if (view?.phase === 'stopping') return <span className="muted">{view.text}</span>;
  const failed = view?.phase === 'error' ? view : null;
  return (
    <>
      {failed === null ? null : <span className="error">{failed.text}</span>}
      {failed?.canForce === true ? (
        <Button onClick={() => props.stop(true)}>Force stop</Button>
      ) : null}
      <Button onClick={() => props.stop(false)}>Stop run</Button>
    </>
  );
}

/**
 * A host path field that completes as the user types: a `Combobox` whose options are the
 * directories the host listed for the typed directory (`completion.ts`).
 */
export function PathInput(
  props: {
    readonly field: CompletionField;
    readonly host: HostKey;
    readonly value: string;
    readonly completion: CompletionState;
    readonly complete: (action: CompletionAction) => void;
    readonly change: (value: string) => void;
    /** Paths to offer besides the completions (recent projects). */
    readonly offers?: readonly string[];
  } & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'list'>,
): JSX.Element {
  const {field, host, value, completion, complete, change, offers = [], ...input} = props;
  return (
    <Combobox
      {...input}
      listId={`${field}-completions`}
      value={value}
      options={[...suggestions(completion, field), ...offers]}
      onFocus={() => complete({type: 'input', field, host, value})}
      change={next => {
        change(next);
        complete({type: 'input', field, host, value: next});
      }}
    />
  );
}
