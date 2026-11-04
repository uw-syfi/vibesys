import type {InputHTMLAttributes, JSX} from 'react';

/**
 * A text field that offers `options` as the user types: a native combobox (`<input list>`), so
 * the platform draws the list and handles the keys. Each option is offered once, in order.
 */
export function Combobox(
  props: {
    /** Names the option list; unique on the page. */
    readonly listId: string;
    readonly value: string;
    readonly options: readonly string[];
    readonly change: (value: string) => void;
  } & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'list'>,
): JSX.Element {
  const {listId, value, options, change, ...input} = props;
  return (
    <>
      <input
        spellCheck={false}
        autoComplete="off"
        {...input}
        value={value}
        list={listId}
        onChange={event => change(event.target.value)}
      />
      <datalist id={listId}>
        {[...new Set(options)].map(option => (
          <option key={option} value={option} />
        ))}
      </datalist>
    </>
  );
}
