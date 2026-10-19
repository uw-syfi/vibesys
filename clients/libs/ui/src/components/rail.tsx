import type {JSX, ReactNode} from 'react';
import {Icon, type IconName} from './icon.js';
import {Text} from './text.js';

/**
 * The app window's layout: a narrow rail of destinations down the left edge, always visible, and
 * the main area beside it, which holds a `Page`. `top` and `bottom` are the rail's two groups of
 * `RailItem`s, the first from the top and the second from the bottom. The rail starts below the
 * window's own controls (`--chrome-inset-height`, which the desktop app sets before the first
 * paint): a band across the top of the window of that height, which holds the window's
 * controls and drags the window, and the rail and the page start below it. `label` names the rail
 * for assistive technology.
 */
export function RailFrame(props: {
  readonly label: string;
  readonly top: ReactNode;
  readonly bottom?: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-rail-frame">
      <div className="vs-rail-band" />
      <div className="vs-rail-body">
        <nav className="vs-rail" aria-label={props.label}>
          <div className="vs-rail-group">{props.top}</div>
          {props.bottom === undefined ? null : (
            <div className="vs-rail-group" data-end>
              {props.bottom}
            </div>
          )}
        </nav>
        <div className="vs-app-main">
          <div className="vs-app-content">{props.children}</div>
        </div>
      </div>
    </div>
  );
}

/** What names a rail item for assistive technology when its label alone does not. */
function itemName(
  label: string,
  hideLabel: boolean,
  extras: readonly (string | undefined)[],
): string | undefined {
  const said = extras.filter(text => text !== undefined);
  return said.length === 0 && !hideLabel ? undefined : [label, ...said].join(', ');
}

/**
 * A destination of the rail: an icon with its short label under it. `active` marks the current
 * screen. `badge` puts a count on the icon (named `badge.label` for assistive technology) and
 * `warning` a dot on it (named by the text); `hideLabel` leaves the icon alone, for the app's mark.
 */
export function RailItem(props: {
  readonly icon: IconName;
  readonly label: string;
  readonly active?: boolean;
  readonly hideLabel?: boolean;
  readonly badge?: {readonly count: number; readonly label: string};
  readonly warning?: string;
  readonly hint?: string;
  readonly press: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      className="vs-rail-item"
      data-active={props.active === true || undefined}
      aria-current={props.active === true ? 'page' : undefined}
      aria-label={itemName(props.label, props.hideLabel === true, [
        props.badge?.label,
        props.warning,
      ])}
      title={props.hint ?? props.label}
      onClick={props.press}
    >
      <span className="vs-rail-icon">
        <Icon name={props.icon} />
        {props.badge === undefined ? null : (
          <span className="vs-rail-badge" aria-hidden="true">
            {props.badge.count}
          </span>
        )}
        {props.warning === undefined ? null : <span className="vs-rail-dot" aria-hidden="true" />}
      </span>
      {props.hideLabel === true ? null : <Text variant="caption">{props.label}</Text>}
    </button>
  );
}
