/**
 * What the welcome view's presentational components share: where their events go, and the modal
 * sheet. Components render the state they are given and report events; they perform no I/O and
 * hold no state that changes behavior.
 */
import type {InputHTMLAttributes, JSX, MouseEvent, ReactNode, SyntheticEvent} from 'react';
import type {HostKey} from '../../../common/hosts.js';
import type {StopView} from '../../../common/stop-run.js';
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

/** Make `dialog`'s native open state match `open`. */
function syncDialog(dialog: HTMLDialogElement, open: boolean): void {
  if (open && !dialog.open) dialog.showModal();
  if (!open && dialog.open) dialog.close();
}

/**
 * A modal `<dialog>` shown while `open`. `showModal()` gives focus containment, an inert page
 * behind, Esc (the `cancel` event), and focus back to the opener on close. Esc and a press on the
 * backdrop ask to `close`; the store decides whether the sheet closes.
 *
 * The ref callback is a new function on every render, so React calls it after every commit: the
 * dialog follows the store without an effect hook. A dialog the browser closed on its own
 * (Chromium closes it on a second Esc with no user activation in between) opens again while the
 * store still holds the sheet open.
 */
export function Sheet(props: {
  readonly open: boolean;
  readonly label: string;
  readonly close: () => void;
  readonly children: ReactNode;
}): JSX.Element {
  const follow = (dialog: HTMLDialogElement | null): void => {
    if (dialog !== null) syncDialog(dialog, props.open);
  };
  const onCancel = (event: SyntheticEvent<HTMLDialogElement>): void => {
    event.preventDefault();
    props.close();
  };
  const onClose = (event: SyntheticEvent<HTMLDialogElement>): void => {
    if (!props.open) return;
    props.close();
    syncDialog(event.currentTarget, true);
  };
  const onMouseDown = (event: MouseEvent<HTMLDialogElement>): void => {
    if (event.target === event.currentTarget) props.close();
  };
  return (
    <dialog
      ref={follow}
      className="sheet"
      aria-label={props.label}
      onCancel={onCancel}
      onClose={onClose}
      onMouseDown={onMouseDown}
    >
      <div className="card">{props.open ? props.children : null}</div>
    </dialog>
  );
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
        <button type="button" onClick={() => props.stop(true)}>
          Force stop
        </button>
      ) : null}
      <button type="button" onClick={() => props.stop(false)}>
        Stop run
      </button>
    </>
  );
}

/**
 * A host path field that completes as the user types: a native combobox (`<input list>`) whose
 * options are the directories the host listed for the typed directory (`completion.ts`).
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
  const listId = `${field}-completions`;
  const options = [...new Set([...suggestions(completion, field), ...offers])];
  return (
    <>
      <input
        {...input}
        value={value}
        list={listId}
        spellCheck={false}
        autoComplete="off"
        onFocus={() => complete({type: 'input', field, host, value})}
        onChange={event => {
          change(event.target.value);
          complete({type: 'input', field, host, value: event.target.value});
        }}
      />
      <datalist id={listId}>
        {options.map(path => (
          <option key={path} value={path} />
        ))}
      </datalist>
    </>
  );
}
