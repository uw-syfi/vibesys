import {type JSX, type KeyboardEvent, useState} from 'react';
import {useFieldWiring} from './field.js';

/** A value the input offers, with an optional detail shown beside it (a version, a size). */
export interface Completion {
  readonly value: string;
  readonly detail?: string;
}

interface ListState {
  /** The highlighted completion, or null. */
  readonly active: number | null;
  /** Esc hid the list; typing shows it again. */
  readonly dismissed: boolean;
}

const RESET: ListState = {active: null, dismissed: false};

function step(state: ListState, key: string, count: number): ListState | null {
  if (key === 'Escape') return state.dismissed ? null : {active: null, dismissed: true};
  if (key !== 'ArrowDown' && key !== 'ArrowUp') return null;
  if (state.dismissed || state.active === null) {
    return {active: key === 'ArrowDown' ? 0 : count - 1, dismissed: false};
  }
  const delta = key === 'ArrowDown' ? 1 : -1;
  return {active: (state.active + delta + count) % count, dismissed: false};
}

/**
 * A mono text input inside a `Field` that offers `completions` in a list under it: the ARIA
 * combobox pattern. The arrows highlight a completion, Enter or a press takes it (`change` with its
 * value), and Esc hides the list until the next edit.
 */
export function PathInput(props: {
  readonly value: string;
  readonly change: (value: string) => void;
  readonly completions: readonly Completion[];
  readonly placeholder?: string;
}): JSX.Element {
  const wiring = useFieldWiring('PathInput');
  const [state, setState] = useState<ListState>(RESET);
  const {completions, change} = props;
  const listId = `${wiring.id}-completions`;
  const optionId = (index: number): string => `${listId}-${index}`;
  const expanded = completions.length > 0 && !state.dismissed;
  const active = expanded && state.active !== null && state.active < completions.length;
  const take = (completion: Completion): void => {
    setState(RESET);
    change(completion.value);
  };
  const keyDown = (event: KeyboardEvent<HTMLInputElement>): void => {
    const highlighted = active && state.active !== null ? completions[state.active] : undefined;
    if (event.key === 'Enter' && highlighted !== undefined) {
      event.preventDefault();
      take(highlighted);
      return;
    }
    const next = completions.length === 0 ? null : step(state, event.key, completions.length);
    if (next === null) return;
    event.preventDefault();
    setState(next);
  };
  return (
    <div className="vs-path-input">
      <input
        className="vs-input"
        id={wiring.id}
        type="text"
        role="combobox"
        data-mono
        data-mono-id
        value={props.value}
        placeholder={props.placeholder}
        spellCheck={false}
        autoComplete="off"
        aria-autocomplete="list"
        aria-expanded={expanded}
        aria-controls={expanded ? listId : undefined}
        aria-activedescendant={active && state.active !== null ? optionId(state.active) : undefined}
        aria-describedby={wiring.describedBy}
        aria-invalid={wiring.invalid || undefined}
        onChange={event => {
          setState(RESET);
          change(event.target.value);
        }}
        onKeyDown={keyDown}
      />
      {expanded ? (
        <div className="vs-completions" id={listId} role="listbox" aria-labelledby={wiring.labelId}>
          {completions.map((completion, index) => (
            <div
              key={completion.value}
              id={optionId(index)}
              role="option"
              tabIndex={-1}
              className="vs-completion"
              aria-selected={active && state.active === index}
              onMouseDown={event => event.preventDefault()}
              onClick={() => take(completion)}
              onKeyDown={event => {
                if (event.key === 'Enter') take(completion);
              }}
            >
              <span className="vs-completion-value">{completion.value}</span>
              {completion.detail === undefined ? null : (
                <span className="vs-completion-detail">{completion.detail}</span>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
