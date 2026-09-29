/** Choose a project folder from the directories the home server may open. */
import {CornerLeftUp, Folder} from 'lucide-react';
import {useEffect, useRef} from 'react';
import type {FsListing} from '../home-api.js';

export interface FolderPickerProps {
  /** Null while the first listing loads. */
  listing: FsListing | null;
  error: string | null;
  /** Lists a folder; null lists the granted roots. */
  onOpen: (path: string | null) => void;
  onChoose: (path: string) => void;
  onClose: () => void;
}

export function FolderPicker({listing, error, onOpen, onChoose, onClose}: FolderPickerProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current;
    if (node !== null && !node.open) node.showModal();
  }, []);
  const here = listing?.path ?? null;
  return (
    <dialog ref={dialog} className="picker" aria-labelledby="picker-title" onClose={onClose}>
      <h4 id="picker-title" className="mono" title={here ?? undefined}>
        {here ?? 'Folders you can open'}
      </h4>
      <div className="plist">
        {here === null ? null : (
          <button
            type="button"
            className="pit"
            aria-label="Up"
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
            if (here !== null) onChoose(here);
          }}
        >
          Choose this folder
        </button>
      </div>
    </dialog>
  );
}
