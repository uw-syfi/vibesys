import {type JSX, type MouseEvent, type ReactNode, useId, useRef} from 'react';
import {Menu, type MenuHandle, type MenuItem} from './menu.js';
import {Chip, type StatusTone} from './status.js';
import {Text} from './text.js';

/** The latest operator-relevant work shown inside a `SummaryCard`. */
export interface SummaryCardUpdate {
  readonly label: string;
  readonly text: string;
}

/**
 * One full-width operator item: its identity and latest work stay at the start, while its visual
 * summary fills the remaining side. The whole card has one primary action. An optional menu is
 * always visible at the corner and is also opened by a context-menu gesture anywhere on the card.
 */
export function SummaryCard(props: {
  readonly title: string;
  readonly description: string;
  readonly latest: SummaryCardUpdate | null;
  /** A non-interactive visual summary; the card remains the one primary action. */
  readonly chart: ReactNode;
  readonly metadata: string;
  readonly status: {readonly tone: StatusTone; readonly label: string};
  readonly attention?: boolean;
  /** The primary action's accessible name; defaults to `Open <title>`. */
  readonly label?: string;
  readonly hint?: string;
  readonly menu?: readonly MenuItem[];
  readonly press: () => void;
}): JSX.Element {
  const titleId = useId();
  const menu = useRef<MenuHandle>(null);
  const contextMenu = (event: MouseEvent<HTMLElement>): void => {
    if (props.menu === undefined) return;
    event.preventDefault();
    menu.current?.open();
  };
  return (
    <article
      className="vs-summary-card"
      data-attention={props.attention === true || undefined}
      data-menu={props.menu !== undefined || undefined}
      aria-labelledby={titleId}
      onContextMenu={contextMenu}
    >
      <button
        type="button"
        className="vs-summary-card-press"
        aria-label={props.label ?? `Open ${props.title}`}
        title={props.hint}
        onClick={props.press}
      />
      <div className="vs-summary-card-content">
        <div className="vs-summary-card-copy">
          <div className="vs-summary-card-title" title={props.title}>
            <Text variant="item" id={titleId}>
              {props.title}
            </Text>
          </div>
          <div className="vs-summary-card-description" title={props.description}>
            <Text variant="small" tone="muted">
              {props.description}
            </Text>
          </div>
          {props.latest === null ? null : (
            <div className="vs-summary-card-latest">
              <Text variant="section">{props.latest.label}</Text>
              <span className="vs-summary-card-latest-text" title={props.latest.text}>
                <Text variant="body-strong">{props.latest.text}</Text>
              </span>
            </div>
          )}
          <div className="vs-summary-card-footer">
            <span className="vs-summary-card-metadata" title={props.metadata}>
              <Text variant="small" tone="muted">
                {props.metadata}
              </Text>
            </span>
            <Chip label={props.status.label} tone={props.status.tone} dot />
          </div>
        </div>
        <div className="vs-summary-card-chart">{props.chart}</div>
      </div>
      {props.menu === undefined ? null : (
        <div className="vs-summary-card-menu">
          <Menu
            ref={menu}
            icon="more"
            label={`More for ${props.title}`}
            variant="quiet"
            size="sm"
            items={props.menu}
          />
        </div>
      )}
    </article>
  );
}
