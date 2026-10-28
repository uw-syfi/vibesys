import type {JSX, ReactNode} from 'react';
import {Button} from './button.js';
import {Menu, type MenuItem} from './menu.js';
import {Chip, StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** A tile's title with its facts line under it: the title gives way first when the row is short. */
function Head(props: {readonly title: string; readonly facts: string}): JSX.Element {
  return (
    <span className="vs-overview-tile-head">
      <Text variant="item">{props.title}</Text>
      <span className="vs-overview-tile-line" title={props.facts}>
        <Text variant="small" tone="muted">
          {props.facts}
        </Text>
      </span>
    </span>
  );
}

function CornerMenu(props: {
  readonly title: string;
  readonly items: readonly MenuItem[] | undefined;
}): JSX.Element | null {
  return props.items === undefined ? null : (
    <span className="vs-overview-tile-menu">
      <Menu
        icon="more"
        label={`More for ${props.title}`}
        variant="quiet"
        size="sm"
        items={props.items}
      />
    </span>
  );
}

/**
 * One subject at a glance as a tile: its `title` with the `facts` line under it, its best
 * `result` with a caption that says what the figure is, a `chart` (none without chart data), and a
 * foot with a status dot and a `summary` of where the subject stands (the whole of it, `summaryHint`,
 * on the pointer). Pressing it opens the subject; `menu` adds a row menu in its corner.
 */
export function OverviewTile(props: {
  readonly title: string;
  readonly facts: string;
  readonly result: {
    readonly text: string;
    readonly tone: 'ok' | 'muted';
    readonly caption: string;
  } | null;
  readonly chart: ReactNode;
  /** The dot's tone, and its word for assistive technology. */
  readonly status: {readonly tone: StatusTone; readonly label: string};
  readonly summary: string;
  readonly summaryHint?: string;
  readonly hint?: string;
  readonly menu?: readonly MenuItem[];
  readonly press: () => void;
}): JSX.Element {
  return (
    <article className="vs-overview-tile" data-menu={props.menu !== undefined || undefined}>
      <button
        type="button"
        className="vs-overview-tile-press"
        title={props.hint}
        onClick={props.press}
      >
        <Head title={props.title} facts={props.facts} />
        {props.result === null ? null : (
          <span className="vs-overview-tile-result">
            <Text variant="metric" tone={props.result.tone === 'ok' ? 'ok' : 'muted'}>
              {props.result.text}
            </Text>
            <Text variant="small" tone="muted">
              {props.result.caption}
            </Text>
          </span>
        )}
        {props.chart === null ? null : (
          <span className="vs-overview-tile-chart">{props.chart}</span>
        )}
        <span className="vs-overview-tile-foot" title={props.summaryHint ?? props.summary}>
          <StatusDot tone={props.status.tone} label={props.status.label} />
          <span className="vs-overview-tile-line">
            <Text variant="small">{props.summary}</Text>
          </span>
        </span>
      </button>
      <CornerMenu title={props.title} items={props.menu} />
    </article>
  );
}

/** What a person can do about a subject that needs them, as a button under its card. */
export interface AttentionAction {
  readonly text: string;
  readonly press: () => void;
}

/**
 * One subject that needs its user, as a card: its `title` with the `facts` line under it and its
 * `status` pill beside them, `why` it needs them in a sentence (three lines at most, the whole on
 * the pointer), and what they can do about it (`actions`). The card's border is the pill's tone.
 * Pressing the card opens the subject; `menu` adds a row menu in its corner.
 */
export function AttentionCard(props: {
  readonly title: string;
  readonly facts: string;
  readonly status: {readonly tone: 'warn' | 'err'; readonly label: string};
  readonly why: string;
  readonly actions: readonly AttentionAction[];
  readonly hint?: string;
  readonly menu?: readonly MenuItem[];
  readonly press: () => void;
}): JSX.Element {
  return (
    <article
      className="vs-overview-tile"
      data-attention={props.status.tone}
      data-menu={props.menu !== undefined || undefined}
    >
      <button
        type="button"
        className="vs-overview-tile-press"
        title={props.hint}
        onClick={props.press}
      >
        <span className="vs-attention-card-top">
          <Head title={props.title} facts={props.facts} />
          <Chip label={props.status.label} tone={props.status.tone} />
        </span>
        <span className="vs-attention-card-why" title={props.why}>
          <Text>{props.why}</Text>
        </span>
      </button>
      {props.actions.length === 0 ? null : (
        <span className="vs-attention-card-actions">
          {props.actions.map(action => (
            <Button
              key={action.text}
              variant="secondary"
              size="sm"
              text={action.text}
              press={action.press}
            />
          ))}
        </span>
      )}
      <CornerMenu title={props.title} items={props.menu} />
    </article>
  );
}

/**
 * A titled group of tiles on one grid that wraps to the width: a small status dot (when `tone` is
 * given), the group's `title` and the count of its tiles (when the title does not already count
 * them), and an `aside` at the end of the head (a key to the tiles' charts). `wide` gives each
 * column the room of a card (`AttentionCard`) rather than a tile.
 */
export function OverviewGroup(props: {
  readonly title: string;
  readonly count?: number;
  readonly tone?: StatusTone;
  readonly aside?: ReactNode;
  readonly wide?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <section className="vs-overview-group" aria-label={props.title}>
      <div className="vs-overview-group-head">
        {props.tone === undefined ? null : <StatusDot tone={props.tone} label={props.title} />}
        <Text variant="section">{props.title}</Text>
        {props.count === undefined ? null : <Text variant="section">{String(props.count)}</Text>}
        {props.aside === undefined ? null : (
          <span className="vs-overview-group-aside">{props.aside}</span>
        )}
      </div>
      <div className="vs-overview-tiles" data-wide={props.wide === true || undefined}>
        {props.children}
      </div>
    </section>
  );
}
