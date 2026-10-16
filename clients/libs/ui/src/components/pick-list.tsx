import {createContext, type FocusEvent, type JSX, type ReactNode, useContext, useId} from 'react';
import {type Guide, Guides} from './guides.js';
import {optionKeys} from './listbox.js';
import {Meter} from './meter.js';
import {Chip, StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

const SelectedRow = createContext<string | undefined>(undefined);

/** One trailing column of a list's header: its name, `bar` wide as a row's bar. */
export interface HeaderColumn {
  readonly label: string;
  readonly bar?: boolean;
}

/** The one-line caption of a list's columns: `start` over the names, `end` over the trailing columns. */
export interface ListHeader {
  readonly start: string;
  readonly end?: readonly HeaderColumn[];
}

function Header(props: {readonly header: ListHeader; readonly id: string}): JSX.Element {
  return (
    <div className="vs-listheader" id={props.id}>
      <span className="vs-listheader-start">{props.header.start}</span>
      {(props.header.end ?? []).map(column => (
        <span
          key={column.label}
          className="vs-listheader-col"
          data-bar={column.bar === true || undefined}
        >
          {column.label}
        </span>
      ))}
    </div>
  );
}

/**
 * A list of `PickRow`s to choose from: a listbox with one tab stop, the `selected` row's id, or
 * the list itself, which passes the focus to its first row. Up and Down move the focus, Home and
 * End jump to the ends, Enter and Space pick. `header` is an optional small line over the rows
 * naming their columns (it describes the list to assistive technology).
 */
export function PickList(props: {
  readonly label: string;
  readonly selected?: string;
  readonly header?: ListHeader;
  readonly children: ReactNode;
}): JSX.Element {
  const headerId = useId();
  const focusFirst = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target === event.currentTarget) {
      event.currentTarget.querySelector<HTMLElement>('[role="option"]')?.focus();
    }
  };
  return (
    <SelectedRow.Provider value={props.selected}>
      <div className="vs-picklist">
        {props.header === undefined ? null : <Header header={props.header} id={headerId} />}
        <div
          className="vs-picklist-rows"
          role="listbox"
          aria-label={props.label}
          aria-describedby={props.header === undefined ? undefined : headerId}
          tabIndex={props.selected === undefined ? 0 : -1}
          onFocus={focusFirst}
        >
          {props.children}
        </div>
      </div>
    </SelectedRow.Provider>
  );
}

/** The share of a row's `bar`, in a tone. */
export interface RowBar {
  readonly value: number;
  readonly tone: 'accent' | 'ok' | 'muted';
  readonly label: string;
}

/**
 * One row of a `PickList`, one line (28 px in a compact screen): what it is (`title`), its most
 * important line (`summary`, which gives way first when the row is narrow), `badges`, a `bar`, a
 * trailing `figure` (a gain, a duration) in a tone, and its `state` (a dot that names the state on
 * hover and when the row has the focus). `guides` draw a lineage tree before the row, one cell per ancestor level, and `lit`
 * marks the cells on the path the reader follows. `fresh` flashes the row once (it changed while
 * on screen). The row whose id is the list's `selected` keeps a bar and a fill.
 */
export function PickRow(props: {
  readonly id: string;
  readonly title: string;
  readonly summary?: string;
  readonly figure?: string;
  readonly figureTone?: 'default' | 'ok' | 'err' | 'muted';
  readonly lead?: string;
  readonly state?: {readonly tone: StatusTone; readonly label: string};
  readonly fresh?: boolean;
  readonly mono?: boolean;
  readonly guides?: readonly Guide[];
  /** On the path from the selected or pointed-at row back to the root: guides in accent, a faint fill. */
  readonly path?: boolean;
  /** Called when the pointer or the focus enters (true) or leaves (false) the row. */
  readonly hover?: (on: boolean) => void;
  readonly bar?: RowBar;
  readonly badges?: readonly {readonly label: string; readonly tone: StatusTone}[];
  readonly select: () => void;
}): JSX.Element {
  const selected = props.id === useContext(SelectedRow);
  const hover = props.hover;
  return (
    <div
      className="vs-pickrow"
      role="option"
      tabIndex={selected ? 0 : -1}
      aria-selected={selected}
      data-fresh={props.fresh === true || undefined}
      data-path={props.path === true || undefined}
      onClick={props.select}
      onMouseEnter={hover === undefined ? undefined : () => hover(true)}
      onMouseLeave={hover === undefined ? undefined : () => hover(false)}
      onFocus={hover === undefined ? undefined : () => hover(true)}
      onBlur={hover === undefined ? undefined : () => hover(false)}
      onKeyDown={event => {
        optionKeys(event, props.select);
      }}
    >
      <Guides guides={props.guides} />
      {props.state === undefined ? null : (
        <span className="vs-pickrow-dot" data-tip={props.state.label}>
          <StatusDot tone={props.state.tone} label={props.state.label} />
        </span>
      )}
      {props.lead === undefined ? null : (
        <span className="vs-pickrow-lead">
          <Text variant="code" tone="muted">
            {props.lead}
          </Text>
        </span>
      )}
      <span className="vs-pickrow-text">
        <Text variant={props.mono === true ? 'code' : 'body-strong'}>{props.title}</Text>
        {props.summary === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.summary}
          </Text>
        )}
      </span>
      {(props.badges ?? []).map(badge => (
        <Chip key={badge.label} label={badge.label} tone={badge.tone} />
      ))}
      {props.bar === undefined ? null : (
        <span className="vs-pickrow-bar">
          <Meter label={props.bar.label} value={props.bar.value} tone={props.bar.tone} />
        </span>
      )}
      {props.figure === undefined ? null : (
        <span className="vs-pickrow-figure" data-tone={props.figureTone ?? 'default'}>
          {props.figure}
        </span>
      )}
    </div>
  );
}
