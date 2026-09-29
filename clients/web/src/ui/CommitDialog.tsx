/** Commit the task files, and only them, after the user has seen the list. */
import type {CommitPreview} from '../home-api.js';
import {useModal} from './FolderPicker.js';

export interface CommitDialogProps {
  /** Null while the preview loads. */
  preview: CommitPreview | null;
  error: string | null;
  busy: boolean;
  onCommit: () => void;
  onClose: () => void;
}

export function CommitDialog({preview, error, busy, onCommit, onClose}: CommitDialogProps) {
  const dialog = useModal();
  const files = preview?.task_files ?? [];
  const other = preview?.other ?? [];
  return (
    <dialog
      ref={dialog}
      className="confirm commit"
      aria-labelledby="commit-title"
      onClose={onClose}
    >
      <h4 id="commit-title">Commit the task files?</h4>
      <p>
        Runs start from a commit so each round can revert cleanly. Only these files are committed:
      </p>
      {preview === null ? (
        <p className="t2">Loading…</p>
      ) : (
        <ul className="files mono">
          {files.map(file => (
            <li key={file}>{file}</li>
          ))}
        </ul>
      )}
      {other.length === 0 ? null : (
        <p className="t2" title={other.join('\n')}>
          {`${other.length} other changed ${other.length === 1 ? 'file stays' : 'files stay'} uncommitted.`}
        </p>
      )}
      {error === null ? null : (
        <pre className="err" role="alert">
          {error}
        </pre>
      )}
      <div className="row">
        {/* First focusable, so showModal() focuses it: a confirmation opens on its safe choice. */}
        <button type="button" className="btn ghost" onClick={() => dialog.current?.close()}>
          Cancel
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={busy || files.length === 0}
          onClick={onCommit}
        >
          {busy ? 'Committing…' : 'Commit'}
        </button>
      </div>
    </dialog>
  );
}
