import {ArrowUp} from 'lucide-react';
import {type ReactNode, useRef, useState} from 'react';

export interface ComposerProps {
  id: string;
  label: string;
  placeholder: string;
  draft: string;
  onDraft: (text: string) => void;
  disabled: boolean;
  /** Typing stays open but Send waits (an answer on this thread is pending). */
  held?: boolean;
  error: string | null;
  /** Resolves true when the text was taken; the draft clears then, unless it changed meanwhile. */
  onSend: (text: string) => Promise<boolean>;
  /** Controls between the input and Send (Ask's model chip). */
  children?: ReactNode;
}

export function Composer(props: ComposerProps) {
  const {id, label, placeholder, draft, onDraft, disabled, held = false, error, onSend} = props;
  const [sending, setSending] = useState(false);
  const latest = useRef(draft);
  latest.current = draft;
  const ready = draft.trim() !== '' && !disabled && !held && !sending;
  async function send() {
    if (!ready) return;
    const submitted = draft;
    setSending(true);
    try {
      // Text typed while the acknowledgment was pending stays.
      if ((await onSend(submitted.trim())) && latest.current === submitted) onDraft('');
    } finally {
      setSending(false);
    }
  }
  return (
    <>
      <div className={disabled ? 'pill off' : 'pill'}>
        <input
          id={id}
          aria-label={label}
          placeholder={placeholder}
          value={draft}
          disabled={disabled}
          onChange={event => onDraft(event.target.value)}
          onKeyDown={event => {
            // Safari ends an IME composition before the Enter that commits it; keyCode 229 marks it.
            const composing = event.nativeEvent.isComposing || event.keyCode === 229;
            if (event.key === 'Enter' && !composing) void send();
          }}
        />
        {props.children}
        <button
          type="button"
          className={ready ? 'send ready' : 'send'}
          aria-label="Send"
          title={held ? 'Waiting for the answer' : 'Send (↵)'}
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
    </>
  );
}
