import type {JSX} from 'react';
import {Icon} from './icon.js';

/**
 * A pill over the end of a scrolling list that says what arrived out of view (`text`, for example
 * "3 new") and brings it into view when pressed.
 */
export function NewBelow(props: {readonly text: string; readonly press: () => void}): JSX.Element {
  return (
    <button type="button" className="vs-new-below" onClick={props.press}>
      <Icon name="chevron-down" />
      {props.text}
    </button>
  );
}
