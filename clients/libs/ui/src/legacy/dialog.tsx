import {type JSX, type MouseEvent, type ReactNode, type SyntheticEvent, useRef} from 'react';

/** Make `dialog`'s native open state match `open`. */
function syncDialog(dialog: HTMLDialogElement, open: boolean): void {
  if (open && !dialog.open) dialog.showModal();
  if (!open && dialog.open) dialog.close();
}

function activeElement(): HTMLElement | null {
  // An initially open dialog has no opener; let its named fallback receive focus.
  return document.activeElement instanceof HTMLElement && document.activeElement !== document.body
    ? document.activeElement
    : null;
}

function restoreFocus(opener: HTMLElement | null, fallbackId: string | undefined): void {
  const fallback = fallbackId === undefined ? null : document.getElementById(fallbackId);
  if (opener?.isConnected === true) opener.focus();
  else if (fallback?.isConnected === true) fallback.focus();
}

/**
 * A modal `<dialog>` shown while `open`. `showModal()` gives focus containment, an inert page
 * behind, Esc (the `cancel` event), and focus back to the opener on close. Esc and a press on the
 * backdrop ask to `dismiss`; the caller's state decides whether the dialog closes.
 *
 * The ref callback is a new function on every render, so React calls it after every commit: the
 * dialog follows `open` without an effect hook. A dialog the browser closed on its own (Chromium
 * closes it on a second Esc with no user activation in between) opens again while `open` holds.
 */
export function Dialog(props: {
  readonly open: boolean;
  readonly label: string;
  readonly dismiss: () => void;
  readonly children: ReactNode;
  /** Stable focus target used when a redraw removes the element that opened the dialog. */
  readonly focusFallbackId?: string;
}): JSX.Element {
  const opener = useRef<HTMLElement | null>(null);
  const fallbackId = useRef<string | undefined>(undefined);
  const follow = (dialog: HTMLDialogElement | null): void => {
    if (dialog === null) return;
    if (props.open && !dialog.open) {
      opener.current = activeElement();
      fallbackId.current = props.focusFallbackId;
      dialog.showModal();
    } else if (!props.open && dialog.open) {
      dialog.close();
      restoreFocus(opener.current, fallbackId.current);
      opener.current = null;
      fallbackId.current = undefined;
    }
  };
  const onCancel = (event: SyntheticEvent<HTMLDialogElement>): void => {
    event.preventDefault();
    props.dismiss();
  };
  const onClose = (event: SyntheticEvent<HTMLDialogElement>): void => {
    if (!props.open) return;
    props.dismiss();
    syncDialog(event.currentTarget, true);
  };
  const onMouseDown = (event: MouseEvent<HTMLDialogElement>): void => {
    if (event.target === event.currentTarget) props.dismiss();
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
