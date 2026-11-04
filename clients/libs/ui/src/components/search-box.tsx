import type {JSX} from 'react';
import {Icon} from './icon.js';

/** A one-line search field for filtering a list, named by `label`. */
export function SearchBox(props: {
  readonly label: string;
  readonly value: string;
  readonly change: (value: string) => void;
  readonly placeholder?: string;
}): JSX.Element {
  return (
    <span className="vs-searchbox">
      <Icon name="search" />
      <input
        type="search"
        aria-label={props.label}
        value={props.value}
        placeholder={props.placeholder}
        spellCheck={false}
        autoComplete="off"
        onChange={event => props.change(event.currentTarget.value)}
      />
    </span>
  );
}
