import {type JSX, type MouseEvent, type ReactNode, type SyntheticEvent, useId, useRef} from 'react';
import {Text} from './text.js';

function focusedElement(): HTMLElement | null {
  return document.activeElement instanceof HTMLElement && document.activeElement !== document.body
    ? document.activeElement
    : null;
}

/**
 * A modal sheet shown while `open`, titled by `title`, with its content and an actions row. The
 * native `<dialog>` gives focus containment, an inert page behind it and Esc; Esc and a press on the
 * backdrop ask to `dismiss`, and the caller's state decides whether it closes. On close the focus
 * goes back to the element that had it when the dialog opened.
 *
 * The ref callback is a new function on every render, so React calls it after every commit and the
 * dialog follows `open` without an effect hook.
 */
export function Dialog(props: {
  readonly open: boolean;
  readonly title: string;
  readonly dismiss: () => void;
  readonly children: ReactNode;
  readonly actions?: ReactNode;
}): JSX.Element {
  const titleId = useId();
  const opener = useRef<HTMLElement | null>(null);
  const follow = (dialog: HTMLDialogElement | null): void => {
    if (dialog === null) return;
    if (props.open && !dialog.open) {
      opener.current = focusedElement();
      dialog.showModal();
    } else if (!props.open && dialog.open) {
      dialog.close();
      if (opener.current?.isConnected === true) opener.current.focus();
      opener.current = null;
    }
  };
  const cancel = (event: SyntheticEvent<HTMLDialogElement>): void => {
    event.preventDefault();
    props.dismiss();
  };
  const closed = (event: SyntheticEvent<HTMLDialogElement>): void => {
    if (!props.open) return;
    props.dismiss();
    event.currentTarget.showModal();
  };
  const backdrop = (event: MouseEvent<HTMLDialogElement>): void => {
    if (event.target === event.currentTarget) props.dismiss();
  };
  return (
    <dialog
      ref={follow}
      className="vs-dialog"
      aria-labelledby={titleId}
      onCancel={cancel}
      onClose={closed}
      onMouseDown={backdrop}
    >
      {props.open ? (
        <div className="vs-dialog-card">
          <Text as="h2" variant="heading" id={titleId}>
            {props.title}
          </Text>
          <div className="vs-dialog-body">{props.children}</div>
          {props.actions === undefined ? null : (
            <div className="vs-dialog-actions">{props.actions}</div>
          )}
        </div>
      ) : null}
    </dialog>
  );
}
