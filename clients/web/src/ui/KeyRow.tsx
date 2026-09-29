/** The selected provider's key: write-only. A typed key lives in this field until it is saved. */
import {useState} from 'react';
import {copyText} from '../clipboard.js';
import type {KeyView} from '../setup.js';
import {Hint, Row} from './Setup.js';

export interface KeyRowProps {
  view: KeyView;
  value: string;
  saving: boolean;
  onValue: (value: string) => void;
  onSave: () => void;
  /** Reads key status again (after a terminal sign-in). */
  onRecheck: () => void;
}

/** The terminal sign-in command, copied on click. */
function LoginCommand({command}: {command: string}) {
  const [copied, setCopied] = useState<boolean | null>(null);
  return (
    <>
      <button
        id="f-key"
        type="button"
        className="linkish mono"
        title={`Copy ${command}`}
        onClick={() => void copyText(command).then(setCopied)}
      >
        {command}
      </button>
      {copied === null ? null : (
        <span aria-live="polite">{copied ? 'Copied' : "Couldn't copy"}</span>
      )}
    </>
  );
}

export function KeyRow({view, value, saving, onValue, onSave, onRecheck}: KeyRowProps) {
  if (view.name === null) {
    return (
      <Row label={view.label} htmlFor={null}>
        <Hint tone={view.tone}>
          {view.hint}
          {view.login === null ? null : (
            <>
              <LoginCommand command={view.login} />
              <button type="button" className="linkbtn" onClick={onRecheck}>
                Check again
              </button>
            </>
          )}
        </Hint>
      </Row>
    );
  }
  return (
    <Row label={view.label} htmlFor="f-key">
      <div className={view.tone === 'bad' ? 'fld keyfld bad' : 'fld keyfld'} title={view.where}>
        <input
          id="f-key"
          type="password"
          autoComplete="new-password"
          spellCheck={false}
          placeholder={view.placeholder}
          value={value}
          disabled={saving}
          onChange={event => onValue(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && value !== '') onSave();
          }}
        />
        {saving ? <span className="spin" aria-hidden /> : null}
        {!saving && value !== '' ? (
          <button type="button" className="linkbtn" onClick={onSave}>
            Save
          </button>
        ) : null}
      </div>
      <Hint tone={view.tone}>{view.hint}</Hint>
    </Row>
  );
}
