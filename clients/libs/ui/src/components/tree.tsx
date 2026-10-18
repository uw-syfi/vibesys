import {
  createContext,
  type FocusEvent,
  type JSX,
  type KeyboardEvent,
  type ReactNode,
  useContext,
} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Icon} from './icon.js';
import type {StatusTone} from './status.js';
import {Chip, StatusDot} from './status.js';
import {Text} from './text.js';

const ROW = '[role="treeitem"]';
const SelectedRow = createContext<string | undefined>(undefined);

function rowsOf(tree: Element | null): HTMLElement[] {
  return tree === null ? [] : [...tree.querySelectorAll<HTMLElement>(ROW)];
}

function level(row: HTMLElement): number {
  return Number(row.getAttribute('aria-level'));
}

/** The row before `index` one level up: the group `index` sits in, or -1 at the top. */
function parentIndex(rows: readonly HTMLElement[], index: number): number {
  const own = level(rows[index] as HTMLElement);
  for (let at = index - 1; at >= 0; at--) {
    if (level(rows[at] as HTMLElement) < own) return at;
  }
  return -1;
}

/**
 * A tree of `TreeRow`s, flat in the document with each row's depth as its level. One tab stop: the
 * `selected` row, or the tree itself, which passes the focus to its first row. Up and Down move;
 * Right opens a closed group or moves into an open one; Left closes an open group or moves to the
 * row's group; Home and End jump to the ends; Enter and Space select.
 */
export function Tree(props: {
  readonly label: string;
  readonly selected?: string;
  readonly children: ReactNode;
}): JSX.Element {
  const focusFirst = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target === event.currentTarget) rowsOf(event.currentTarget)[0]?.focus();
  };
  return (
    <SelectedRow.Provider value={props.selected}>
      <div
        className="vs-tree"
        role="tree"
        aria-label={props.label}
        tabIndex={props.selected === undefined ? 0 : -1}
        onFocus={focusFirst}
      >
        {props.children}
      </div>
    </SelectedRow.Provider>
  );
}

/** A trailing chip of a row: a short label in a tone. */
export interface RowChip {
  readonly label: string;
  readonly tone?: StatusTone;
}

/**
 * One row of a `Tree`, indented by `depth`. A group has `expanded` (and `toggle`) and shows a
 * disclosure chevron; a leaf may show a state dot. `chips` trail the title.
 */
export function TreeRow(props: {
  readonly id: string;
  readonly depth: number;
  readonly title: string;
  readonly subtitle?: string;
  readonly state?: {readonly tone: StatusTone; readonly label: string};
  readonly chips?: readonly RowChip[];
  readonly expanded?: boolean;
  readonly toggle?: (expanded: boolean) => void;
  readonly select: () => void;
}): JSX.Element {
  const selected = useContext(SelectedRow);
  const group = props.expanded !== undefined;
  const keyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const rows = rowsOf(event.currentTarget.closest('[role="tree"]'));
    const index = rows.indexOf(event.currentTarget);
    const focus = (at: number): void => rows[at]?.focus();
    switch (event.key) {
      case 'ArrowDown':
        focus(Math.min(rows.length - 1, index + 1));
        break;
      case 'ArrowUp':
        focus(Math.max(0, index - 1));
        break;
      case 'Home':
        focus(0);
        break;
      case 'End':
        focus(rows.length - 1);
        break;
      case 'ArrowRight':
        if (props.expanded === false) props.toggle?.(true);
        else if (props.expanded === true) focus(index + 1);
        break;
      case 'ArrowLeft':
        if (props.expanded === true) props.toggle?.(false);
        else focus(parentIndex(rows, index));
        break;
      case 'Enter':
      case ' ':
        props.select();
        break;
      default:
        return;
    }
    event.preventDefault();
  };
  const indent: StyleWithVars = {'--tree-depth': props.depth};
  return (
    <div
      className="vs-tree-row"
      role="treeitem"
      aria-level={props.depth + 1}
      aria-expanded={props.expanded}
      aria-selected={props.id === selected}
      data-group={group || undefined}
      tabIndex={props.id === selected ? 0 : -1}
      style={indent}
      onClick={() => {
        if (group) props.toggle?.(!props.expanded);
        props.select();
      }}
      onKeyDown={keyDown}
    >
      {group ? (
        <span className="vs-tree-chevron" data-open={props.expanded || undefined}>
          <Icon name="chevron-right" />
        </span>
      ) : props.state === undefined ? null : (
        <StatusDot tone={props.state.tone} label={props.state.label} />
      )}
      <span className="vs-tree-text">
        <Text variant={group ? 'body' : 'item'} tone={group ? 'muted' : 'default'}>
          {props.title}
        </Text>
        {props.subtitle === undefined ? null : (
          <Text variant={group ? 'code' : 'small'} tone="muted">
            {props.subtitle}
          </Text>
        )}
      </span>
      {(props.chips ?? []).map(chip => (
        <Chip
          key={chip.label}
          label={chip.label}
          {...(chip.tone === undefined ? {} : {tone: chip.tone})}
        />
      ))}
    </div>
  );
}
