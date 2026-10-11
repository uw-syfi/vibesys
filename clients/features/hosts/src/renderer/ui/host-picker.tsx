/** "Connect to host": search `~/.ssh/config` aliases or type a destination; arrows and Enter pick. */

import {Button, Dialog} from '@vibesys/ui';
import type {JSX, KeyboardEvent} from 'react';
import {type PickerAction, type PickerState, pickerChoices} from '../../state/picker.js';

function emptyText(state: Extract<PickerState, {kind: 'open'}>): string {
  return state.aliases.kind === 'loading' ? 'Reading ~/.ssh/config…' : 'No matching hosts.';
}

function Choices(props: {
  readonly state: Extract<PickerState, {kind: 'open'}>;
  readonly picker: (action: PickerAction) => void;
}): JSX.Element {
  const {state, picker} = props;
  const choices = pickerChoices(state);
  if (choices.length === 0) return <p className="muted">{emptyText(state)}</p>;
  return (
    <div className="list" role="listbox" aria-label="Hosts">
      {choices.map((choice, index) => (
        <Button
          key={`${choice.typed ? 'typed' : 'config'}:${choice.alias}`}
          role="option"
          tabIndex={-1}
          aria-selected={index === state.selected}
          className="option"
          onClick={() => picker({type: 'choose', index})}
        >
          <span>{choice.alias}</span>
          <span className="muted">
            {choice.typed ? 'connect to this destination' : 'ssh config'}
          </span>
        </Button>
      ))}
    </div>
  );
}

export function HostPicker(props: {
  readonly state: PickerState;
  /** False while a run fills the window. */
  readonly visible: boolean;
  readonly picker: (action: PickerAction) => void;
}): JSX.Element {
  const {state, picker} = props;
  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      picker({type: 'move', delta: event.key === 'ArrowDown' ? 1 : -1});
    } else if (event.key === 'Enter') {
      picker({type: 'choose'});
    }
  };
  return (
    <Dialog
      open={state.kind === 'open' && props.visible}
      label="Connect to host"
      dismiss={() => picker({type: 'close'})}
    >
      {state.kind !== 'open' ? null : (
        <>
          <input
            // biome-ignore lint/a11y/noAutofocus: the sheet is a search box; typing starts at once.
            autoFocus
            className="search"
            placeholder="Search hosts, or type user@host"
            spellCheck={false}
            autoComplete="off"
            value={state.query}
            onChange={event => picker({type: 'query', value: event.target.value})}
            onKeyDown={onKeyDown}
          />
          {state.aliases.kind === 'failed' ? <p className="error">{state.aliases.error}</p> : null}
          <Choices state={state} picker={picker} />
        </>
      )}
    </Dialog>
  );
}
