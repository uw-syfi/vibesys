import {type KeyboardEvent, type RefObject, useEffect, useRef, useState} from 'react';
import {filterPalette, type PaletteItem} from '../palette.js';

const GROUPS: ReadonlyArray<PaletteItem['group']> = ['Run', 'Go to', 'Agent'];

export interface PaletteProps {
  items: PaletteItem[];
  onRun: (item: PaletteItem) => void;
  onClose: () => void;
}

/**
 * Opens as a native modal dialog: focus is trapped and Escape (the dialog's own cancel) closes it. Focus returns to the
 * element that had it when the palette opened, unless running a command moved it elsewhere.
 */
function useModal() {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const opener = document.activeElement;
    const element = dialog.current;
    if (element !== null && !element.open) element.showModal();
    input.current?.focus();
    return () => {
      if (document.activeElement === document.body && opener instanceof HTMLElement) opener.focus();
    };
  }, []);
  return {dialog, input};
}

/** Whether rows sit below the list's visible end: the list fades there and the count says so. */
function useMore(list: RefObject<HTMLDivElement | null>, shown: readonly PaletteItem[]) {
  const [more, setMore] = useState(false);
  const measure = () => {
    const node = list.current;
    if (node !== null) setMore(node.scrollHeight - node.scrollTop - node.clientHeight > 1);
  };
  // After the modal opens (its effect runs first): a closed dialog has no height to measure.
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new result set changes the height.
  useEffect(measure, [shown]);
  return {more, measure};
}

function PaletteList({
  list,
  more,
  onScroll,
  shown,
  current,
  onActive,
  onRun,
}: {
  list: RefObject<HTMLDivElement | null>;
  more: boolean;
  onScroll: () => void;
  shown: PaletteItem[];
  current: PaletteItem | undefined;
  onActive: (index: number) => void;
  onRun: (item: PaletteItem) => void;
}) {
  return (
    <div
      ref={list}
      id="palette-list"
      className={more ? 'list more' : 'list'}
      role="listbox"
      aria-label="Commands"
      onScroll={onScroll}
    >
      {GROUPS.map(group => {
        const members = shown.filter(entry => entry.group === group);
        if (members.length === 0) return null;
        return (
          <div key={group}>
            <div className="gh">{group}</div>
            {members.map(entry => (
              <button
                key={entry.id}
                id={`pal-${entry.id}`}
                type="button"
                role="option"
                tabIndex={-1}
                aria-selected={entry === current}
                className={entry === current ? 'it on' : 'it'}
                onMouseEnter={() => onActive(shown.indexOf(entry))}
                onClick={() => onRun(entry)}
              >
                <span className="lbl">{entry.label}</span>
                {entry.detail === '' ? null : <span className="d">{entry.detail}</span>}
                {entry.keys === '' ? null : <span className="kbd">{entry.keys}</span>}
              </button>
            ))}
          </div>
        );
      })}
      {shown.length === 0 ? <div className="gh">No matching commands</div> : null}
    </div>
  );
}

export function Palette({items, onRun, onClose}: PaletteProps) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const modal = useModal();
  const shown = filterPalette(items, query);
  const list = useRef<HTMLDivElement>(null);
  const {more, measure} = useMore(list, shown);
  const current = shown[Math.min(active, shown.length - 1)];
  useEffect(() => {
    if (current !== undefined)
      document.getElementById(`pal-${current.id}`)?.scrollIntoView({block: 'nearest'});
  }, [current]);
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActive(index => Math.max(0, Math.min(shown.length - 1, index + step)));
    } else if (event.key === 'Enter' && current !== undefined) {
      onRun(current);
    }
  };
  return (
    <dialog ref={modal.dialog} className="pal" aria-label="Search and commands" onClose={onClose}>
      <input
        ref={modal.input}
        role="combobox"
        aria-expanded="true"
        aria-controls="palette-list"
        aria-activedescendant={current === undefined ? undefined : `pal-${current.id}`}
        placeholder="Search commands, rounds and views…"
        value={query}
        onChange={event => {
          setQuery(event.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
      />
      <PaletteList
        list={list}
        more={more}
        onScroll={measure}
        shown={shown}
        current={current}
        onActive={setActive}
        onRun={onRun}
      />
      <div className="foot">
        <span className="kbd">↑↓ move</span>
        <span className="kbd">↵ run</span>
        <span className="kbd">esc close</span>
        <span className="kbd count">
          {shown.length} results{more ? ', scroll for more' : ''}
        </span>
      </div>
    </dialog>
  );
}
