import {type JSX, useId} from 'react';

interface ToggleProps {
  readonly label: string;
  readonly checked: boolean;
  readonly change: (checked: boolean) => void;
  /** Disables the toggle and says why, as help text under it that it is described by. */
  readonly disabledReason?: string;
}

function Toggle(props: ToggleProps & {readonly kind: 'checkbox' | 'switch'}): JSX.Element {
  const reasonId = useId();
  const disabled = props.disabledReason !== undefined;
  return (
    <span className="vs-toggle">
      <label className="vs-toggle-label">
        <input
          className={props.kind === 'switch' ? 'vs-switch' : 'vs-checkbox'}
          type="checkbox"
          role={props.kind === 'switch' ? 'switch' : undefined}
          checked={props.checked}
          disabled={disabled}
          aria-describedby={disabled ? reasonId : undefined}
          onChange={event => props.change(event.target.checked)}
        />
        <span>{props.label}</span>
      </label>
      {disabled ? (
        <span className="vs-help" id={reasonId}>
          {props.disabledReason}
        </span>
      ) : null}
    </span>
  );
}

/** A labelled check box. */
export function Checkbox(props: ToggleProps): JSX.Element {
  return <Toggle {...props} kind="checkbox" />;
}

/** A labelled on and off switch, for a setting that applies at once. */
export function Switch(props: ToggleProps): JSX.Element {
  return <Toggle {...props} kind="switch" />;
}
