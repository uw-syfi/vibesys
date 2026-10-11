/**
 * What the welcome view's presentational components share: where their events go, and the modal
 * sheet. Components render the state they are given and report events; they perform no I/O and
 * hold no state that changes behavior.
 */
import {type JSX, type MouseEvent, type ReactNode, useEffect, useRef} from 'react';
import type {StopView} from '../../stop-run.js';
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
}

export type Dispatch = (command: RunCommand) => void;

/** Send `command` (attach, resume, stop, ...) to the welcome store. */
export function commands(send: WelcomeSenders): Dispatch {
  return command => send.welcome({type: 'command', command});
}

/**
 * A modal `<dialog>` shown while `open`. `showModal()` gives focus containment, an inert page
 * behind, Esc (the `cancel` event), and focus back to the opener on close. Esc and a press on the
 * backdrop ask to `close`; the store decides whether the sheet closes.
 */
export function Sheet(props: {
  readonly open: boolean;
  readonly label: string;
  readonly close: () => void;
  readonly children: ReactNode;
}): JSX.Element {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (props.open && !dialog.open) dialog.showModal();
    if (!props.open && dialog.open) dialog.close();
  }, [props.open]);
  const onCancel = (event: {preventDefault(): void}): void => {
    event.preventDefault();
    props.close();
  };
  const onMouseDown = (event: MouseEvent<HTMLDialogElement>): void => {
    if (event.target === ref.current) props.close();
  };
  return (
    <dialog
      ref={ref}
      className="sheet"
      aria-label={props.label}
      onCancel={onCancel}
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
