import type {JSX, ReactNode} from 'react';
import {Icon, type IconName} from './icon.js';
import {Menu, type MenuItem} from './menu.js';
import {StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** Where a sidebar entry goes: a link, or an action. */
export type SidebarTarget =
  | {readonly href: string; readonly press?: never}
  | {readonly press: () => void; readonly href?: never};

/**
 * A navigation entry: an icon or a status dot, the label, an optional count, and an optional detail
 * line under the label. `active` marks the current page. `menu` adds a row menu beside it.
 */
export function SidebarItem(
  props: SidebarTarget & {
    readonly label: string;
    readonly icon?: IconName;
    readonly status?: {readonly tone: StatusTone; readonly label: string};
    readonly count?: number;
    readonly detail?: string;
    readonly active?: boolean;
    readonly menu?: readonly MenuItem[];
  },
): JSX.Element {
  const content = (
    <>
      {props.icon === undefined ? null : <Icon name={props.icon} />}
      {props.status === undefined ? null : (
        <span className="vs-sidebar-dot">
          <StatusDot tone={props.status.tone} label={props.status.label} />
        </span>
      )}
      <span className="vs-sidebar-text">
        <Text variant={props.detail === undefined ? 'body' : 'body-strong'}>{props.label}</Text>
        {props.detail === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.detail}
          </Text>
        )}
      </span>
      {props.count === undefined ? null : <Text variant="section">{String(props.count)}</Text>}
    </>
  );
  const shared = {
    className: 'vs-sidebar-link',
    'aria-current': props.active === true ? ('page' as const) : undefined,
  };
  return (
    <div className="vs-sidebar-item" data-active={props.active === true || undefined}>
      {props.href !== undefined ? (
        <a {...shared} href={props.href}>
          {content}
        </a>
      ) : (
        <button {...shared} type="button" onClick={props.press}>
          {content}
        </button>
      )}
      {props.menu === undefined ? null : (
        <Menu
          icon="more"
          label={`More for ${props.label}`}
          variant="quiet"
          size="sm"
          items={props.menu}
        />
      )}
    </div>
  );
}

/** A titled group of sidebar entries, with an optional "Show all" link after them. */
export function SidebarSection(props: {
  readonly title: string;
  readonly children: ReactNode;
  readonly showAll?: SidebarTarget & {readonly label: string};
}): JSX.Element {
  const {showAll} = props;
  return (
    <section className="vs-sidebar-section" aria-label={props.title}>
      <span className="vs-sidebar-title">
        <Text variant="section">{props.title}</Text>
      </span>
      {props.children}
      {showAll === undefined ? null : showAll.href !== undefined ? (
        <a className="vs-sidebar-more" href={showAll.href}>
          {showAll.label}
        </a>
      ) : (
        <button className="vs-sidebar-more" type="button" onClick={showAll.press}>
          {showAll.label}
        </button>
      )}
    </section>
  );
}
