import type {JSX, ReactNode} from 'react';
import {Icon, type IconName} from './icon.js';
import {Menu, type MenuItem} from './menu.js';
import {StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** Where a sidebar entry goes: a link, or an action. */
export type SidebarTarget =
  | {readonly href: string; readonly press?: never}
  | {readonly press: () => void; readonly href?: never};

/** What leads an entry: its icon, status dot or step marker. */
function Lead(props: {
  readonly icon: IconName | undefined;
  readonly status: {readonly tone: StatusTone; readonly label: string} | undefined;
  readonly marker: MarkerProps | undefined;
}): JSX.Element {
  return (
    <>
      {props.icon === undefined ? null : <Icon name={props.icon} />}
      {props.status === undefined ? null : (
        <span className="vs-sidebar-dot">
          <StatusDot tone={props.status.tone} label={props.status.label} />
        </span>
      )}
      {props.marker === undefined ? null : <Marker {...props.marker} />}
    </>
  );
}

/** The numbered circle of a step in a list of steps. */
interface MarkerProps {
  readonly text: string;
  readonly state: 'current' | 'neutral' | 'muted';
  readonly label: string;
}

function Marker(props: MarkerProps): JSX.Element {
  return (
    <span
      className="vs-sidebar-marker"
      data-state={props.state}
      role="img"
      aria-label={props.label}
    >
      {props.text}
    </span>
  );
}

/**
 * A navigation entry: an icon, a status dot or a numbered marker, the label, an optional count, and an optional detail
 * line under the label. `active` marks the current page. `menu` adds a row menu beside it.
 */
export function SidebarItem(
  props: SidebarTarget & {
    readonly label: string;
    readonly icon?: IconName;
    readonly status?: {readonly tone: StatusTone; readonly label: string};
    /** A number in a small circle in place of a status dot: `current` is filled, `muted` is faint. */
    readonly marker?: MarkerProps;
    readonly count?: number;
    readonly detail?: string;
    /** For entries under a group's header: the label may wrap, and `trail` follows it, muted. */
    readonly compact?: boolean;
    readonly trail?: string;
    /** What assistive technology calls the entry when its text alone does not say what it is of. */
    readonly name?: string;
    /** What its menu is for, when that is not the label. */
    readonly menuFor?: string;
    /** The tooltip of the whole entry: what its text cannot hold. */
    readonly hint?: string;
    /** A control beside the entry (retry), outside its button. */
    readonly action?: {readonly label: string; readonly press: () => void};
    readonly active?: boolean;
    /** An entry that cannot be followed yet: drawn muted and not pressable. */
    readonly disabled?: boolean;
    readonly menu?: readonly MenuItem[];
  },
): JSX.Element {
  const content = (
    <>
      <Lead icon={props.icon} status={props.status} marker={props.marker} />
      <span className="vs-sidebar-text" data-compact={props.compact === true || undefined}>
        <Text
          variant={props.detail === undefined && props.compact !== true ? 'body' : 'body-strong'}
        >
          {props.label}
        </Text>
        {props.trail === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.trail}
          </Text>
        )}
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
    title: props.hint,
    'aria-label': props.name,
    'aria-current': props.active === true ? ('page' as const) : undefined,
  };
  return (
    <div
      className="vs-sidebar-item"
      data-active={props.active === true || undefined}
      data-disabled={props.disabled === true || undefined}
    >
      {props.href !== undefined ? (
        <a {...shared} href={props.href}>
          {content}
        </a>
      ) : (
        <button {...shared} type="button" disabled={props.disabled} onClick={props.press}>
          {content}
        </button>
      )}
      {props.menu === undefined ? null : (
        <Menu
          icon="more"
          label={`More for ${props.menuFor ?? props.label}`}
          variant="quiet"
          size="sm"
          items={props.menu}
        />
      )}
      {props.action === undefined ? null : (
        <>
          <span className="vs-sidebar-break" aria-hidden="true" />
          <button className="vs-sidebar-action" type="button" onClick={props.action.press}>
            {props.action.label}
          </button>
        </>
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

/**
 * A small header over the entries of one kind, with `note` (how many follow) at its end when given. The entries are a list under it.
 */
export function SidebarGroup(props: {
  readonly title: string;
  readonly note?: string;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-sidebar-group">
      <span className="vs-sidebar-group-title" title={props.title}>
        <Text variant="small" tone="muted">
          {props.title}
        </Text>
        {props.note === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.note}
          </Text>
        )}
      </span>
      {props.children}
    </div>
  );
}
