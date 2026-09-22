import {ArrowUp, Clock} from 'lucide-react';
import {useState} from 'react';
import type {PendingSteer} from '../model.js';
import './Composer.css';

export interface ComposerProps {
  pending: readonly PendingSteer[];
  /** Disconnected: typing stays possible, sending does not, and focus is kept. */
  disabled: boolean;
  /** "Steer failed: …" until the next command. */
  error: string | null;
  onSend: (text: string) => Promise<boolean>;
}

export function Composer({pending, disabled, error, onSend}: ComposerProps) {
  const [text, setText] = useState('');
  async function send() {
    if (disabled || text.trim() === '') return;
    // Text typed while the send was in flight stays.
    if (await onSend(text.trim())) setText(current => (current === text ? '' : current));
  }

  return (
    <div className="dock">
      <div className="col">
        {pending.map(steer => (
          <p key={steer.id} className="qline">
            <Clock size={16} strokeWidth={1.75} aria-hidden />
            <span className="q">Queued</span>
            <span className="qt" data-tip={steer.text}>
              {steer.text}
            </span>
          </p>
        ))}
        <form
          className="cmp"
          onSubmit={event => {
            event.preventDefault();
            void send();
          }}
        >
          <span className="prompt" aria-hidden="true">
            &gt;
          </span>
          <label className="sr-only" htmlFor="steer">
            Steer the run
          </label>
          <textarea
            id="steer"
            rows={1}
            autoComplete="off"
            placeholder="Guide the next agent call"
            aria-keyshortcuts="/"
            value={text}
            onChange={event => setText(event.target.value)}
            onKeyDown={event => {
              // Safari ends the composition before the Enter that commits it; keyCode 229 marks it.
              const composing = event.nativeEvent.isComposing || event.keyCode === 229;
              if (event.key === 'Enter' && !event.shiftKey && !composing) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <button
            type="submit"
            className="btn btn-primary btn-icon"
            aria-label="Send"
            aria-disabled={disabled || undefined}
            data-tip="Send"
            data-key="Enter"
          >
            <ArrowUp size={16} strokeWidth={1.75} aria-hidden />
          </button>
        </form>
        {error === null ? null : (
          <p className="steer-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
