import {
  createContext,
  type JSX,
  type KeyboardEvent,
  type ReactNode,
  useContext,
  useId,
} from 'react';

/** What a `Field` gives the one control inside it: its id, what describes it, and its validity. */
export interface FieldWiring {
  readonly id: string;
  /** The label's id, for a popup the control owns (a list of completions) to be named by. */
  readonly labelId: string;
  readonly describedBy: string | undefined;
  readonly invalid: boolean;
}

const FieldContext = createContext<FieldWiring | null>(null);

/** The wiring of the enclosing `Field`. A control outside one has no label, so this throws. */
export function useFieldWiring(control: string): FieldWiring {
  const wiring = useContext(FieldContext);
  if (wiring === null) {
    throw new Error(`${control} must be inside a Field, which gives it its label, help and error`);
  }
  return wiring;
}

/**
 * A label, one control (`TextInput`, `PathInput` or `Select`), and help and error text the control
 * is described by. With `error` the control is marked invalid.
 */
export function Field(props: {
  readonly label: string;
  readonly help?: string;
  /** What the field needs explained, shown as the label's tooltip instead of a line of help. */
  readonly hint?: string;
  readonly error?: string;
  /** The longer account behind a short `error` (a status word), shown on the pointer. */
  readonly errorHint?: string;
  /** Names the control to assistive technology and no one else, for a control in a toolbar row. */
  readonly hideLabel?: boolean;
  /** The label beside the control instead of above it, for a form read as rows (a review). */
  readonly row?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const id = useId();
  const helpId = props.help === undefined ? undefined : `${id}-help`;
  const errorId = props.error === undefined ? undefined : `${id}-error`;
  const describedBy = [helpId, errorId].filter(Boolean).join(' ') || undefined;
  const labelId = `${id}-label`;
  const wiring: FieldWiring = {id, labelId, describedBy, invalid: errorId !== undefined};
  return (
    <div className="vs-field" data-row={props.row === true || undefined}>
      <label
        className="vs-field-label"
        id={labelId}
        htmlFor={id}
        title={props.hint}
        data-hidden={props.hideLabel === true || undefined}
      >
        {props.label}
      </label>
      <FieldContext.Provider value={wiring}>{props.children}</FieldContext.Provider>
      {helpId === undefined ? null : (
        <p className="vs-help" id={helpId}>
          {props.help}
        </p>
      )}
      {errorId === undefined ? null : (
        <p className="vs-help" data-tone="err" id={errorId} title={props.errorHint}>
          {props.error}
        </p>
      )}
    </div>
  );
}

/**
 * A term beside what it says, laid out as a `Field` with `row` but around no one control: a value,
 * a value and a button, or a control that names itself. It is a description list, not a
 * `<label>`, so it points at nothing it cannot focus.
 */
export function LabeledRow(props: {
  readonly label: string;
  readonly help?: string;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <dl className="vs-field" data-row>
      <dt className="vs-field-label">{props.label}</dt>
      <dd className="vs-field-value">
        {props.children}
        {props.help === undefined ? null : <p className="vs-help">{props.help}</p>}
      </dd>
    </dl>
  );
}

/**
 * A text input; `mono` for paths and identifiers, `readOnly` for a value shown as set. With
 * `rows` above one it is a multi-line box of that height. `submit` is called on Enter; in a multi-line
 * box Shift+Enter still starts a new line.
 */
export function TextInput(props: {
  readonly value: string;
  readonly change?: (value: string) => void;
  readonly placeholder?: string;
  readonly mono?: boolean;
  readonly readOnly?: boolean;
  readonly rows?: number;
  readonly submit?: () => void;
}): JSX.Element {
  const wiring = useFieldWiring('TextInput');
  const shared = {
    className: 'vs-input',
    id: wiring.id,
    value: props.value,
    placeholder: props.placeholder,
    readOnly: props.readOnly,
    spellCheck: false,
    autoComplete: 'off',
    'data-mono': props.mono === true || undefined,
    'data-mono-id': props.mono === true || undefined,
    'aria-describedby': wiring.describedBy,
    'aria-invalid': wiring.invalid || undefined,
  } as const;
  const {submit} = props;
  const onEnter =
    submit === undefined
      ? undefined
      : (event: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>): void => {
          if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
          event.preventDefault();
          submit();
        };
  if (props.rows !== undefined && props.rows > 1) {
    return (
      <textarea
        {...shared}
        rows={props.rows}
        data-multiline
        onKeyDown={onEnter}
        onChange={event => props.change?.(event.target.value)}
      />
    );
  }
  return (
    <input
      {...shared}
      type="text"
      onKeyDown={onEnter}
      onChange={event => props.change?.(event.target.value)}
    />
  );
}
