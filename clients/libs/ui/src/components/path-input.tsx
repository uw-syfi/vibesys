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

/** A ref callback with one identity for good, so React calls it when the input mounts and never again. */
const focusOnMount = (input: HTMLInputElement | null): void => input?.focus();

/** What the owner is called for on a key the input has no completion to use it on: Enter submits, Escape closes. */
function ownerOf(
  key: string,
  owner: {readonly submit?: () => void; readonly escape?: () => void},
): (() => void) | undefined {
  if (key === 'Enter') return owner.submit;
  return key === 'Escape' ? owner.escape : undefined;
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
  /** Takes the completion a person picks (Enter or a press) in place of `change` with its value. */
  readonly pick?: (completion: Completion) => void;
  /** Takes the focus when it appears. */
  readonly takeFocus?: boolean;
  /** Called on Escape when the list of completions was not open to close first. */
  readonly escape?: () => void;
  /** Called on Enter when no completion is highlighted. */
  readonly submit?: () => void;
}): JSX.Element {
  const wiring = useFieldWiring('PathInput');
  const [state, setState] = useState<ListState>(RESET);
  const [focusFirst] = useState(props.takeFocus === true);
  const {completions, change} = props;
  const listId = `${wiring.id}-completions`;
  const optionId = (index: number): string => `${listId}-${index}`;
  const expanded = completions.length > 0 && !state.dismissed;
  const active = expanded && state.active !== null && state.active < completions.length;
  const take = (completion: Completion): void => {
    setState(RESET);
    if (props.pick === undefined) change(completion.value);
    else props.pick(completion);
  };
  const keyDown = (event: KeyboardEvent<HTMLInputElement>): void => {
    const highlighted = active && state.active !== null ? completions[state.active] : undefined;
    if (event.key === 'Enter' && highlighted !== undefined) {
      event.preventDefault();
      take(highlighted);
      return;
    }
    const next = completions.length === 0 ? null : step(state, event.key, completions.length);
    if (next !== null) {
      event.preventDefault();
      setState(next);
      return;
    }
    // Enter with nothing highlighted, and Escape with no list to close, are the owner's.
    const own = ownerOf(event.key, props);
    if (own === undefined) return;
    event.preventDefault();
    // The owner may remove this input; the key must not then reach the page's own Escape (back).
    event.stopPropagation();
    own();
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
        ref={focusFirst ? focusOnMount : undefined}
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
