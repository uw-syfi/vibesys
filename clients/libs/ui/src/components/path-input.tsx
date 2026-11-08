import {type JSX, type KeyboardEvent, useState} from 'react';
import {useFieldWiring} from './field.js';
import {ScrollArea} from './scroll-area.js';

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
 * The completion a key takes, if any: Enter takes the highlighted one; Tab takes it too, or with
 * `tabCompletes` the first when none is highlighted.
 */
function takenBy(
  event: {readonly key: string; readonly shiftKey: boolean},
  list: {
    readonly open: boolean;
    readonly highlighted: Completion | undefined;
    readonly first: Completion | undefined;
    readonly tabCompletes: boolean;
  },
): Completion | undefined {
  if (event.key === 'Enter') return list.highlighted;
  if (event.key !== 'Tab' || event.shiftKey || !list.open) return undefined;
  return list.highlighted ?? (list.tabCompletes ? list.first : undefined);
}

/** The options of the open list, each taken by a press. */
function Options(props: {
  readonly id: string;
  readonly labelId: string;
  readonly completions: readonly Completion[];
  readonly active: number | null;
  readonly optionId: (index: number) => string;
  readonly take: (completion: Completion) => void;
}): JSX.Element {
  return (
    <ScrollArea
      kind="feed"
      className="vs-completions"
      id={props.id}
      role="listbox"
      aria-labelledby={props.labelId}
    >
      {props.completions.map((completion, index) => (
        <div
          key={completion.value}
          id={props.optionId(index)}
          role="option"
          tabIndex={-1}
          className="vs-completion"
          aria-selected={props.active === index}
          onMouseDown={event => event.preventDefault()}
          onClick={() => props.take(completion)}
          onKeyDown={event => {
            if (event.key === 'Enter') props.take(completion);
          }}
        >
          <span className="vs-completion-value">{completion.value}</span>
          {completion.detail === undefined ? null : (
            <span className="vs-completion-detail">{completion.detail}</span>
          )}
        </div>
      ))}
    </ScrollArea>
  );
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
  /** Called when the input takes the focus. */
  readonly focus?: () => void;
  /**
   * Tab takes the highlighted completion; with `tabCompletes` and none highlighted it takes the
   * first (the value ends in the middle of a name), and otherwise Tab moves on as it does anywhere.
   */
  readonly tabCompletes?: boolean;
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
    const taken = takenBy(event, {
      open: expanded,
      highlighted,
      first: completions[0],
      tabCompletes: props.tabCompletes === true,
    });
    if (taken !== undefined) {
      event.preventDefault();
      take(taken);
      return;
    }
    const next = completions.length === 0 ? null : step(state, event.key, completions.length);
    if (next !== null) {
      event.preventDefault();
      setState(next);
      if (next.active !== null)
        event.currentTarget.ownerDocument
          .getElementById(optionId(next.active))
          ?.scrollIntoView?.({block: 'nearest'});
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
        onFocus={props.focus}
      />
      {expanded ? (
        <Options
          id={listId}
          labelId={wiring.labelId}
          completions={completions}
          active={active ? state.active : null}
          optionId={optionId}
          take={take}
        />
      ) : null}
    </div>
  );
}
