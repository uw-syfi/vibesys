import {type JSX, type MouseEvent, type ReactNode, useId, useRef} from 'react';
import {Menu, type MenuHandle, type MenuItem} from './menu.js';
import {Meter} from './meter.js';
import {Chip, type StatusTone} from './status.js';
import {Text} from './text.js';

/** The latest operator-relevant work shown inside a `SummaryCard`. */
export interface SummaryCardUpdate {
  readonly label: string;
  readonly text: string;
}

/**
 * One labelled figure in a card's figure strip: what it counts, its value, and an optional quiet
 * line under it (a limit, a time left). A `share` draws how far along a limit it is, in `tone`.
 */
export interface SummaryStat {
  readonly label: string;
  readonly value: string;
  /** A quiet line under the value: the other half of a ratio, or what the value is measured against. */
  readonly detail?: string;
  /** The whole account, for the pointer. */
  readonly hint?: string;
  /** How far along a limit the value is, 0 to 1; drawn as a thin meter under the figure. */
  readonly share?: number;
  /** The tone of the meter, and of the value when it is a problem. */
  readonly tone?: 'default' | 'ok' | 'warn' | 'muted';
}

/** A short reason the card wants a look, drawn as a chip beside its status. */
export interface SummaryFlag {
  readonly label: string;
  readonly tone: StatusTone;
  readonly hint?: string;
}

/** The headline result drawn over the chart: what it is, its value, and how it compares. */
export interface SummaryResult {
  readonly label: string;
  readonly value: string;
  readonly detail?: string;
  readonly hint?: string;
}

function Stat(props: {readonly stat: SummaryStat}): JSX.Element {
  const {stat} = props;
  const tone = stat.tone ?? 'default';
  return (
    <div className="vs-summary-card-stat" title={stat.hint}>
      <Text variant="section">{stat.label}</Text>
      <span className="vs-summary-card-stat-value">
        <Text
          variant="body-strong"
          tone={tone === 'warn' ? 'warn' : tone === 'muted' ? 'muted' : 'default'}
        >
          {stat.value}
        </Text>
      </span>
      {stat.share === undefined ? null : (
        <Meter
          decorative
          label={`${stat.label} ${(stat.share * 100).toFixed(0)}%`}
          value={stat.share}
          tone={tone === 'warn' ? 'warn' : tone === 'ok' ? 'ok' : 'accent'}
        />
      )}
      {stat.detail === undefined ? null : (
        <span className="vs-summary-card-stat-detail">
          <Text variant="caption" tone="muted">
            {stat.detail}
          </Text>
        </span>
      )}
    </div>
  );
}

/**
 * One full-width operator item: its identity, status and latest work stay at the start, a strip of
 * labelled figures sits under them, and its visual summary fills the remaining side. The whole card
 * has one primary action. An optional menu is always visible at the corner and is also opened by a
 * context-menu gesture anywhere on the card.
 */
export function SummaryCard(props: {
  readonly title: string;
  /** One line the author wrote; null leaves the line out rather than filling it with a stand-in. */
  readonly description: string | null;
  readonly latest: SummaryCardUpdate | null;
  /** A non-interactive visual summary; the card remains the one primary action. */
  readonly chart: ReactNode;
  /** Where the item lives, in the quiet voice under its figures. */
  readonly metadata: string;
  readonly status: {readonly tone: StatusTone; readonly label: string};
  /** Why the item wants a look, beside its status. */
  readonly flags?: readonly SummaryFlag[];
  /** The figures that tell items of one kind apart; a figure nobody reported is left out. */
  readonly stats?: readonly SummaryStat[];
  /** The headline result, drawn over the chart's corner. */
  readonly result?: SummaryResult | null;
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
  const stats = props.stats ?? [];
  const flags = props.flags ?? [];
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
          <div className="vs-summary-card-head">
            <Chip label={props.status.label} tone={props.status.tone} dot />
            {flags.map(flag => (
              <Chip
                key={flag.label}
                label={flag.label}
                tone={flag.tone}
                {...(flag.hint === undefined ? {} : {hint: flag.hint})}
              />
            ))}
          </div>
          <div className="vs-summary-card-title" title={props.title}>
            <Text variant="item" id={titleId}>
              {props.title}
            </Text>
          </div>
          {props.description === null ? null : (
            <div className="vs-summary-card-description" title={props.description}>
              <Text variant="small" tone="muted">
                {props.description}
              </Text>
            </div>
          )}
          {props.latest === null ? null : (
            <div className="vs-summary-card-latest">
              <Text variant="section">{props.latest.label}</Text>
              <span className="vs-summary-card-latest-text" title={props.latest.text}>
                <Text variant="body-strong">{props.latest.text}</Text>
              </span>
            </div>
          )}
          {stats.length === 0 ? null : (
            <div className="vs-summary-card-stats">
              {stats.map(stat => (
                <Stat key={stat.label} stat={stat} />
              ))}
            </div>
          )}
          <div className="vs-summary-card-footer">
            <span className="vs-summary-card-metadata" title={props.metadata}>
              <Text variant="small" tone="muted">
                {props.metadata}
              </Text>
            </span>
          </div>
        </div>
        <div className="vs-summary-card-chart">
          {props.result === undefined || props.result === null ? null : (
            <div className="vs-summary-card-result" title={props.result.hint}>
              <Text variant="section">{props.result.label}</Text>
              <span className="vs-summary-card-result-value">
                <Text variant="item">{props.result.value}</Text>
              </span>
              {props.result.detail === undefined ? null : (
                <Text variant="caption" tone="muted">
                  {props.result.detail}
                </Text>
              )}
            </div>
          )}
          <div className="vs-summary-card-plot">{props.chart}</div>
        </div>
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
