/** Choose a project folder from the directories the home server may open. */
import {CornerLeftUp, Folder} from 'lucide-react';
import {type KeyboardEvent, useEffect, useRef} from 'react';
import type {FsListing} from '../home-api.js';
import {tildePath} from '../setup.js';

export interface FolderPickerProps {
  /** Null while the first listing loads. */
  listing: FsListing | null;
  error: string | null;
  /** Lists a folder; null lists the granted roots. */
  onOpen: (path: string | null) => void;
  onChoose: (path: string) => void;
  onClose: () => void;
}

/**
 * Opens as a native modal dialog: focus is trapped and Escape (the dialog's own cancel) closes it. Focus returns to
 * the element that had it when the picker opened, whether the dialog closes natively or the caller unmounts it.
 */
export function useModal() {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const opener = document.activeElement;
    const node = dialog.current;
    if (node !== null && !node.open) node.showModal();
    return () => {
      if (document.activeElement === document.body && opener instanceof HTMLElement) opener.focus();
    };
  }, []);
  return dialog;
}

/** Arrow keys move focus between the row buttons; Enter/Space activate the focused one natively. */
function onRowKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  event.preventDefault();
  const list = event.currentTarget.parentElement;
  const rows = Array.from(list?.querySelectorAll<HTMLButtonElement>('button.pit') ?? []);
  const index = rows.indexOf(event.currentTarget);
  const step = event.key === 'ArrowDown' ? 1 : -1;
  rows[Math.min(rows.length - 1, Math.max(0, index + step))]?.focus();
}

export function FolderPicker({listing, error, onOpen, onChoose, onClose}: FolderPickerProps) {
  const dialog = useModal();
  const here = listing?.path ?? null;
  return (
    <dialog ref={dialog} className="picker" aria-labelledby="picker-title" onClose={onClose}>
      <h4 id="picker-title" className="mono" title={here ?? undefined}>
        {here === null ? 'Folders you can open' : tildePath(here)}
      </h4>
      <div className="plist">
        {here === null ? null : (
          <button
            type="button"
            className="pit"
            aria-label="Up"
            onKeyDown={onRowKeyDown}
            onClick={() => onOpen(listing?.parent ?? null)}
          >
            <CornerLeftUp size={14} strokeWidth={1.5} aria-hidden />
            <span className="nm">..</span>
          </button>
        )}
        {listing === null && error === null ? <p className="empty1">Loading…</p> : null}
        {(listing?.entries ?? []).map(entry => (
          <button
            key={entry.path}
            type="button"
            className="pit"
            title={entry.path}
            onKeyDown={onRowKeyDown}
            onClick={() => onOpen(entry.path)}
          >
            <Folder size={14} strokeWidth={1.5} aria-hidden />
            <span className="nm">{entry.name}</span>
            {entry.git ? <span className="kbd">git</span> : null}
          </button>
        ))}
        {listing !== null && listing.entries.length === 0 ? (
          <p className="empty1">No folders here.</p>
        ) : null}
      </div>
      {error === null ? null : (
        <p className="hint bad" role="alert">
          {error}
        </p>
      )}
      <div className="row">
        <button type="button" className="btn ghost" onClick={() => dialog.current?.close()}>
          Cancel
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={here === null}
          onClick={() => {
            if (here === null) return;
            dialog.current?.close();
            onChoose(here);
          }}
        >
          Choose this folder
        </button>
      </div>
    </dialog>
  );
}
