/** "Connect to host": search `~/.ssh/config` aliases or type a destination; arrows and Enter pick. */

import {Button, Dialog} from '@vibesys/ui';
import {type JSX, type KeyboardEvent, useId} from 'react';
import {type PickerAction, type PickerState, pickerChoices} from '../../state/picker.js';

function emptyText(state: Extract<PickerState, {kind: 'open'}>): string {
  return state.aliases.kind === 'loading' ? 'Reading ~/.ssh/config…' : 'No matching hosts.';
}

function Choices(props: {
  readonly state: Extract<PickerState, {kind: 'open'}>;
  readonly listboxId: string;
  readonly picker: (action: PickerAction) => void;
}): JSX.Element {
  const {state, listboxId, picker} = props;
  const choices = pickerChoices(state);
  return (
    <>
      <div id={listboxId} className="list" role="listbox" aria-label="Hosts">
        {choices.map((choice, index) => (
          <Button
            id={`${listboxId}-option-${index}`}
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
      {choices.length === 0 ? <p className="muted">{emptyText(state)}</p> : null}
    </>
  );
}

export function HostPicker(props: {
  readonly state: PickerState;
  /** False while a run fills the window. */
  readonly visible: boolean;
  readonly picker: (action: PickerAction) => void;
}): JSX.Element {
  const {state, picker} = props;
  const listboxId = useId();
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
            autoFocus
            className="search"
            role="combobox"
            aria-label="Search hosts"
            aria-expanded={true}
            aria-controls={listboxId}
            aria-autocomplete="list"
            aria-activedescendant={
              pickerChoices(state)[state.selected] === undefined
                ? undefined
                : `${listboxId}-option-${state.selected}`
            }
            placeholder="Search hosts, or type user@host"
            spellCheck={false}
            autoComplete="off"
            value={state.query}
            onChange={event => picker({type: 'query', value: event.target.value})}
            onKeyDown={onKeyDown}
          />
          {state.aliases.kind === 'failed' ? <p className="error">{state.aliases.error}</p> : null}
          <Choices state={state} listboxId={listboxId} picker={picker} />
        </>
      )}
    </Dialog>
  );
}
