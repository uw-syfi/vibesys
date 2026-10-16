import type {TranscriptEntry} from '@vibesys/run-state/transcript';
import {Button, Dialog} from '@vibesys/ui/legacy';
import type {JSX, KeyboardEvent} from 'react';
import type {ConversationEvent, ConversationState, Submission} from '../../state/index.js';

interface Props {
  readonly state: ConversationState;
  readonly dispatch: (event: ConversationEvent) => void;
  readonly target: string;
  readonly unavailableReason: string;
  readonly transcript: readonly TranscriptEntry[];
}

export function ConversationView({
  state,
  dispatch,
  target,
  unavailableReason,
  transcript,
}: Props): JSX.Element {
  const {intent} = state;
  const available = state.connected && (intent === 'ask' || state.steerAvailable);
  const pending = state.submissions.some(
    item => item.intent === intent && item.status === 'pending',
  );
  const draft = state.drafts[intent];
  return (
    <>
      <Button className="conversation-trigger" onClick={() => dispatch({type: 'open', open: true})}>
        Conversation
      </Button>
      <Dialog
        open={state.open}
        label="Conversation"
        dismiss={() => dispatch({type: 'open', open: false})}
      >
        <div className="conversation-panel">
          <div className="dialog-head">
            <div>
              <p className="section-kicker">RUN CONVERSATION</p>
              <h2>Conversation</h2>
            </div>
            <Button
              className="dialog-close"
              aria-label="Close conversation"
              onClick={() => dispatch({type: 'open', open: false})}
            >
              ×
            </Button>
          </div>
          <p className="conversation-target">{target}</p>
          <p className="conversation-intro">
            Ask about this run through experiment chat, or guide the next agent invocation with
            Steer.
          </p>
          <fieldset className="conversation-modes" aria-label="Message intent">
            <Button
              aria-pressed={intent === 'ask'}
              onClick={() => dispatch({type: 'intent', intent: 'ask'})}
            >
              Ask about this run
            </Button>
            <Button
              aria-pressed={intent === 'steer'}
              onClick={() => dispatch({type: 'intent', intent: 'steer'})}
            >
              Steer next invocation
            </Button>
          </fieldset>
          <div
            className="conversation-scroll"
            role="log"
            aria-label="Conversation messages"
            aria-live="polite"
          >
            <ConversationMessages transcript={transcript} submissions={state.submissions} />
          </div>
          <form
            className="conversation-composer"
            onSubmit={event => {
              event.preventDefault();
              dispatch({type: 'submit', transcriptStart: transcript.length});
            }}
          >
            {!available && <p className="conversation-unavailable">{unavailableReason}</p>}
            <label htmlFor="conversation-message">
              {intent === 'ask' ? 'Question about this run' : 'Guidance for the next invocation'}
            </label>
            <textarea
              id="conversation-message"
              name="conversation-message"
              value={draft}
              onChange={event => dispatch({type: 'draft', text: event.target.value})}
              onKeyDown={onComposerKeyDown}
              disabled={!available}
              rows={3}
              placeholder={
                intent === 'ask' ? 'Ask about the run…' : 'Guide the next agent invocation…'
              }
            />
            <div className="conversation-composer-footer">
              <span>Enter to send · Shift+Enter for a new line</span>
              <Button type="submit" disabled={!available || pending || draft.trim().length === 0}>
                {pending ? 'Waiting…' : intent === 'ask' ? 'Ask' : 'Steer'}
              </Button>
            </div>
          </form>
        </div>
      </Dialog>
    </>
  );
}

function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
  if (
    event.key !== 'Enter' ||
    event.shiftKey ||
    event.nativeEvent.isComposing ||
    event.keyCode === 229
  )
    return;
  event.preventDefault();
  event.currentTarget.form?.requestSubmit();
}

function ConversationMessages({
  transcript,
  submissions,
}: {
  readonly transcript: readonly TranscriptEntry[];
  readonly submissions: readonly Submission[];
}): JSX.Element {
  if (transcript.length === 0 && submissions.length === 0)
    return <p className="conversation-empty">No messages in this run yet.</p>;
  const items: JSX.Element[] = [];
  let offset = 0;
  for (const submission of submissions) {
    for (const entry of transcript.slice(offset, submission.transcriptStart)) {
      items.push(<TranscriptMessage key={`core-${entry.id}`} entry={entry} />);
    }
    items.push(
      <article key={`local-${submission.id}`} className="conversation-message conversation-local">
        <span>{submission.intent === 'ask' ? 'You · Ask' : 'You · Steer'}</span>
        <p>{submission.text}</p>
        {submission.status === 'pending' && <small>Waiting for a response…</small>}
        {submission.detail !== null &&
          !transcript
            .slice(submission.transcriptStart)
            .some(entry => entry.content === submission.detail) && (
            <small className={submission.status === 'error' ? 'conversation-error' : ''}>
              {submission.detail}
            </small>
          )}
      </article>,
    );
    offset = submission.transcriptStart;
  }
  for (const entry of transcript.slice(offset))
    items.push(<TranscriptMessage key={`core-${entry.id}`} entry={entry} />);
  return <div className="conversation-messages">{items}</div>;
}
function TranscriptMessage({entry}: {readonly entry: TranscriptEntry}): JSX.Element {
  return (
    <article className="conversation-message conversation-answer">
      <span>{entry.label ?? 'Answer'}</span>
      <p>{entry.content}</p>
    </article>
  );
}
