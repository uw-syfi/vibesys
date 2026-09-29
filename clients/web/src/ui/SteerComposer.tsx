import {ArrowUp} from 'lucide-react';
import {useState} from 'react';

export interface SteerComposerProps {
  disabled: boolean;
  /** Why steering is off, shown as the placeholder; null while it is on. */
  reason: string | null;
  error: string | null;
  /** Resolves true when the backend acknowledged the steer; the draft clears then. */
  onSend: (text: string) => Promise<boolean>;
}

export function SteerComposer({disabled, reason, error, onSend}: SteerComposerProps) {
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const ready = draft.trim() !== '' && !disabled && !sending;
  async function send() {
    if (!ready) return;
    const submitted = draft;
    setSending(true);
    const sent = await onSend(submitted.trim());
    setSending(false);
    // Text typed while the acknowledgment was pending stays.
    if (sent) setDraft(current => (current === submitted ? '' : current));
  }
  return (
    <div className="dock">
      <div className="inner">
        <div className={disabled ? 'pill off' : 'pill'}>
          <input
            id="steer"
            aria-label="Steer the next agent call"
            placeholder={reason ?? 'Steer the next agent call…'}
            value={draft}
            disabled={disabled}
            onChange={event => setDraft(event.target.value)}
            onKeyDown={event => {
              // Safari ends an IME composition before the Enter that commits it; keyCode 229 marks it.
              const composing = event.nativeEvent.isComposing || event.keyCode === 229;
              if (event.key === 'Enter' && !composing) void send();
            }}
          />
          <button
            type="button"
            className={ready ? 'send ready' : 'send'}
            aria-label="Send"
            title="Send (↵)"
            disabled={!ready}
            onClick={() => void send()}
          >
            <ArrowUp size={16} strokeWidth={1.5} aria-hidden />
          </button>
        </div>
        {error === null ? null : (
          <p className="hint bad" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
