import type {JSX} from 'react';
import {Button} from './button.js';

/**
 * The control that shows or hides the longer account behind a short title. It is a button of its
 * own, named by what it does, so the title next to it is never a control that shares a name with
 * an action beside it.
 */
export function DetailsToggle(props: {
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
}): JSX.Element {
  return (
    <Button
      variant="quiet"
      size="sm"
      icon={props.open ? 'chevron-down' : 'chevron-right'}
      label={props.open ? 'Hide details' : 'Show details'}
      expanded={props.open}
      press={() => props.toggle(!props.open)}
    />
  );
}
