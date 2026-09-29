import type {AskView} from '../ask.js';
import type {NoteState} from '../notes.js';
import {PaneHead} from './Pane.js';

export interface NotesTabProps {
  note: NoteState;
  /** The steer composer is shown (the run has not ended). */
  canSteer: boolean;
  /** Ask has a composer only while the run offers a chat harness. */
  harness: AskView['harness'];
  onEdit: (text: string) => void;
  onBlur: () => void;
  onRetry: () => void;
  onSteerDraft: () => void;
  onAskDraft: () => void;
}

export function NotesTab(props: NotesTabProps) {
  return (
    <>
      <PaneHead scope="Run">
        <span>never sent to agents</span>
      </PaneHead>
      <NoteBody {...props} />
    </>
  );
}

function NoteBody(props: NotesTabProps) {
  const {note} = props;
  switch (note.phase) {
    case 'unavailable':
      return (
        <p className="empty1">
          Notes are kept by the VibeSys home server; open this run from the app to use them.
        </p>
      );
    case 'loading':
      return <p className="empty1">Loading…</p>;
    case 'failed':
      return (
        <div className="empty1">
          <p className="bad" role="alert">{`Could not load the note: ${note.message}`}</p>
          <button type="button" className="btn" onClick={props.onRetry}>
            Retry
          </button>
        </div>
      );
    case 'ready':
      return <Editor {...props} text={note.text} error={note.error} />;
  }
}

function Editor(props: NotesTabProps & {text: string; error: string | null}) {
  const {text, error, canSteer, harness} = props;
  const canAsk = harness === 'available';
  const empty = text.trim() === '';
  return (
    <>
      <textarea
        className="notesed"
        aria-label="Notes"
        placeholder="Write notes for this run…"
        value={text}
        onChange={event => props.onEdit(event.target.value)}
        onBlur={props.onBlur}
      />
      <div className="pfoot">
        <button
          type="button"
          className="btn"
          disabled={empty || !canSteer}
          title={
            canSteer ? 'Put this note in the steer composer; nothing is sent' : 'The run has ended'
          }
          onClick={props.onSteerDraft}
        >
          Use as steer draft
        </button>
        <button
          type="button"
          className="btn"
          disabled={empty || !canAsk}
          title={
            canAsk
              ? 'Put this note in the Ask composer; nothing is sent'
              : harness === 'checking'
                ? 'Checking the chat harness…'
                : harness === 'failed'
                  ? "Couldn't check the chat harness"
                  : 'This run offers no chat harness'
          }
          onClick={props.onAskDraft}
        >
          Use as ask draft
        </button>
        {error === null ? null : (
          <span className="hint bad" role="alert">{`Not saved: ${error}`}</span>
        )}
      </div>
    </>
  );
}
