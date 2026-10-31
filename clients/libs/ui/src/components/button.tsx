import {type JSX, type MouseEvent, useId} from 'react';
import {Icon, type IconName} from './icon.js';

/** `primary` is inverted (the one action a surface leads with), `secondary` a hairline outline. */
export type ButtonVariant = 'primary' | 'secondary' | 'quiet' | 'danger';
export type ButtonSize = 'sm' | 'md';

/** Visible text, optionally with an icon; or an icon alone, then named by `label`. */
export type ButtonFace =
  | {
      readonly text: string;
      readonly icon?: IconName;
      /** The accessible name when it says more than the text does ("Retry host-a"); it starts with the text. */
      readonly label?: string;
    }
  | {readonly icon: IconName; readonly label: string; readonly text?: never};

/** What pressing does: call `press`, follow `href` (an `<a>`), or submit the enclosing form. */
export type ButtonAction =
  | {readonly press: () => void; readonly href?: never; readonly submit?: never}
  | {readonly href: string; readonly press?: never; readonly submit?: never}
  | {readonly submit: true; readonly press?: never; readonly href?: never};

export type ButtonProps = ButtonFace &
  ButtonAction & {
    readonly variant?: ButtonVariant;
    readonly size?: ButtonSize;
    /** Fill the container's width. */
    readonly full?: boolean;
    /** Disables the button and says why, as help text under it that the button is described by. */
    readonly disabledReason?: string;
    /** Keep the reason for assistive technology only, with no caption drawn. */
    readonly quietReason?: boolean;
    /** For a button that shows and hides a region: whether that region is shown. */
    readonly expanded?: boolean;
    /** A hover tooltip: what the control does, and its keyboard shortcut. */
    readonly hint?: string;
  };

function Face(props: ButtonFace): JSX.Element {
  return (
    <>
      {props.icon === undefined ? null : <Icon name={props.icon} />}
      {props.text === undefined ? null : <span>{props.text}</span>}
    </>
  );
}

/**
 * A button, or a link drawn as one when it has `href`. A disabled button stays focusable
 * (`aria-disabled`) so its reason can be reached and read; pressing it does nothing.
 */
export function Button(props: ButtonProps): JSX.Element {
  const reasonId = useId();
  const disabled = props.disabledReason !== undefined;
  const shared = {
    className: 'vs-button',
    'data-variant': props.variant ?? 'secondary',
    'data-size': props.size ?? 'md',
    'data-full': props.full === true || undefined,
    'aria-label': props.label,
    title: props.hint,
  };
  if (props.href !== undefined && !disabled) {
    return (
      <a {...shared} href={props.href}>
        <Face {...props} />
      </a>
    );
  }
  const click = (event: MouseEvent<HTMLButtonElement>): void => {
    if (disabled) event.preventDefault();
    else props.press?.();
  };
  const button = (
    <button
      {...shared}
      type={props.submit === true ? 'submit' : 'button'}
      aria-disabled={disabled || undefined}
      aria-describedby={disabled ? reasonId : undefined}
      aria-expanded={props.expanded}
      onClick={click}
    >
      <Face {...props} />
    </button>
  );
  if (!disabled) return button;
  return (
    <span className="vs-button-disabled" data-full={props.full === true || undefined}>
      {button}
      <span className="vs-help" id={reasonId} data-quiet={props.quietReason || undefined}>
        {props.disabledReason}
      </span>
    </span>
  );
}
