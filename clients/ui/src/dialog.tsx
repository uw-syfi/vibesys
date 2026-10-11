import type {JSX, MouseEvent, ReactNode, SyntheticEvent} from 'react';

/** Make `dialog`'s native open state match `open`. */
function syncDialog(dialog: HTMLDialogElement, open: boolean): void {
  if (open && !dialog.open) dialog.showModal();
  if (!open && dialog.open) dialog.close();
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
}): JSX.Element {
  const follow = (dialog: HTMLDialogElement | null): void => {
    if (dialog !== null) syncDialog(dialog, props.open);
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
