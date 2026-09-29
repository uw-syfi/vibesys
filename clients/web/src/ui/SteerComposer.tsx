import {Composer} from './Composer.js';

export interface SteerComposerProps {
  disabled: boolean;
  /** Why steering is off, shown as the placeholder; null while it is on. */
  reason: string | null;
  error: string | null;
  draft: string;
  onDraft: (text: string) => void;
  /** Resolves true when the backend acknowledged the steer; the draft clears then. */
  onSend: (text: string) => Promise<boolean>;
}

export function SteerComposer({reason, ...rest}: SteerComposerProps) {
  return (
    <div className="dock">
      <div className="inner">
        <Composer
          id="steer"
          label="Steer the next agent call"
          placeholder={reason ?? 'Steer the next agent call…'}
          {...rest}
        />
      </div>
    </div>
  );
}
