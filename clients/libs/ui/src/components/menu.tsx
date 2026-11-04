import {
  type FocusEvent,
  type JSX,
  type KeyboardEvent,
  type ReactNode,
  useId,
  useRef,
  useState,
} from 'react';
import type {ButtonFace, ButtonVariant} from './button.js';
import {Icon} from './icon.js';
import {rovingStep} from './roving.js';

/** One entry of a `Menu`: `press` is called, `href` is followed; `danger` draws it in `err`. */
export type MenuItem = {
  readonly id: string;
  readonly label: string;
  readonly danger?: boolean;
} & (
  | {readonly press: () => void; readonly href?: never}
  | {readonly href: string; readonly press?: never}
);

/**
 * A box whose `Menu`s open under it, no wider than it: for a menu in a narrow panel, where the
 * list would otherwise grow past the panel's edge. The menus sit in the box's `Row`s.
 */
export function MenuBounds(props: {readonly children: ReactNode}): JSX.Element {
  return <div className="vs-menu-bounds">{props.children}</div>;
}

const ITEM = '[role="menuitem"]';

function items(list: HTMLElement | null): HTMLElement[] {
  return list === null ? [] : [...list.querySelectorAll<HTMLElement>(ITEM)];
}

/**
 * A button that opens a menu of `items` in a raised panel under it. Enter, Space or the down arrow
 * opens it on the first item; the arrows, Home and End move; Esc closes it and gives the focus back
 * to the button; choosing an item, or moving the focus out, closes it. The panel lines up with
 * the button's `end` edge (the default, for a button at the end of a row) or its `start` edge.
 */
export function Menu(
  props: ButtonFace & {
    readonly items: readonly MenuItem[];
    readonly variant?: ButtonVariant;
    readonly size?: 'sm' | 'md';
    readonly align?: 'start' | 'end';
  },
): JSX.Element {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const firstFocus = useRef<'first' | 'last' | null>(null);
  const list = (element: HTMLDivElement | null): void => {
    const target = firstFocus.current;
    if (element === null || target === null) return;
    firstFocus.current = null;
    const all = items(element);
    (target === 'first' ? all[0] : all.at(-1))?.focus();
  };
  const show = (focus: 'first' | 'last'): void => {
    firstFocus.current = focus;
    setOpen(true);
  };
  const close = (refocus: boolean): void => {
    setOpen(false);
    if (refocus) trigger.current?.focus();
  };
  const triggerKey = (event: KeyboardEvent<HTMLButtonElement>): void => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    show(event.key === 'ArrowDown' ? 'first' : 'last');
  };
  const listKey = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === 'Escape' || event.key === 'Tab') {
      if (event.key === 'Escape') event.preventDefault();
      close(event.key === 'Escape');
      return;
    }
    const all = items(event.currentTarget);
    const index = all.indexOf(document.activeElement as HTMLElement);
    const next = rovingStep(event.key, Math.max(0, index), all.length);
    if (next === null) return;
    event.preventDefault();
    all[next]?.focus();
  };
  const blur = (event: FocusEvent<HTMLElement>): void => {
    if (root.current?.contains(event.relatedTarget as Node | null) !== true) setOpen(false);
  };
  return (
    <div ref={root} className="vs-menu">
      <button
        ref={trigger}
        type="button"
        className="vs-button"
        data-variant={props.variant ?? 'secondary'}
        data-size={props.size ?? 'md'}
        aria-label={props.label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? close(false) : show('first'))}
        onKeyDown={triggerKey}
        onBlur={blur}
      >
        {props.icon === undefined ? null : <Icon name={props.icon} />}
        {props.text === undefined ? null : <span>{props.text}</span>}
      </button>
      {open ? (
        <div
          ref={list}
          id={menuId}
          className="vs-menu-list"
          data-align={props.align ?? 'end'}
          role="menu"
          aria-label={props.label ?? props.text}
          tabIndex={-1}
          onKeyDown={listKey}
          onBlur={blur}
        >
          {props.items.map(item => (
            <MenuEntry key={item.id} item={item} done={() => close(true)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function MenuEntry(props: {readonly item: MenuItem; readonly done: () => void}): JSX.Element {
  const {item} = props;
  const shared = {
    className: 'vs-menu-item',
    role: 'menuitem',
    tabIndex: -1,
    'data-danger': item.danger === true || undefined,
  } as const;
  if (item.href !== undefined) {
    return (
      <a {...shared} href={item.href} title={item.label} onClick={props.done}>
        <span>{item.label}</span>
      </a>
    );
  }
  return (
    <button
      {...shared}
      type="button"
      title={item.label}
      onClick={() => {
        props.done();
        item.press?.();
      }}
    >
      <span>{item.label}</span>
    </button>
  );
}
