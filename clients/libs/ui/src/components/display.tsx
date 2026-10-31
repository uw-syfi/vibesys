import type {JSX, ReactNode} from 'react';
import {Text} from './text.js';

/** One fact: its label and its value; `mono` sets the value in mono. */
export interface KeyValueItem {
  readonly key: string;
  readonly value: ReactNode;
  readonly mono?: boolean;
}

/** Facts as a label column and a value column. */
export function KeyValue(props: {readonly items: readonly KeyValueItem[]}): JSX.Element {
  return (
    <dl className="vs-key-value">
      {props.items.map(item => (
        <div key={item.key} className="vs-key-value-row">
          <dt>
            <Text variant="small" tone="muted">
              {item.key}
            </Text>
          </dt>
          <dd>
            <Text variant={item.mono === true ? 'code' : 'small'}>{item.value}</Text>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** One block of plain prose: a heading, a paragraph, or a list of items. */
export type ProseBlock =
  | {readonly kind: 'heading'; readonly text: string}
  | {readonly kind: 'paragraph'; readonly text: string}
  | {readonly kind: 'list'; readonly ordered: boolean; readonly items: readonly string[]};

/**
 * `values` paired with keys made from their text and how often that text came before, so equal
 * values get distinct keys and a key never depends on position alone.
 */
function keyedByText<T>(values: readonly T[], text: (value: T) => string): [string, T][] {
  const seen = new Map<string, number>();
  return values.map(value => {
    const base = text(value);
    const nth = seen.get(base) ?? 0;
    seen.set(base, nth + 1);
    return [`${base}\u0000${nth}`, value];
  });
}

function ProseItem(props: {readonly block: ProseBlock}): JSX.Element {
  const {block} = props;
  if (block.kind === 'heading') {
    return (
      <Text as="h3" variant="heading">
        {block.text}
      </Text>
    );
  }
  if (block.kind === 'paragraph') return <Text as="p">{block.text}</Text>;
  const List = block.ordered ? 'ol' : 'ul';
  return (
    <List className="vs-prose-list">
      {keyedByText(block.items, item => item).map(([key, item]) => (
        <li key={key}>
          <Text>{item}</Text>
        </li>
      ))}
    </List>
  );
}

/**
 * Text the caller has split into headings, paragraphs and lists, drawn in the type scale. It
 * renders text only, never markup, so authored text (an objective, a description) cannot inject
 * any. Headings are `h3`, under the section that holds the prose.
 */
export function Prose(props: {readonly blocks: readonly ProseBlock[]}): JSX.Element {
  const text = (block: ProseBlock): string =>
    block.kind === 'list'
      ? `${block.kind}:${block.items.join('\n')}`
      : `${block.kind}:${block.text}`;
  return (
    <div className="vs-prose">
      {keyedByText(props.blocks, text).map(([key, block]) => (
        <ProseItem key={key} block={block} />
      ))}
    </div>
  );
}

/**
 * A key number: a `section` label, the value in the `metric` style (with a small `unit` after it),
 * and an optional sub-label.
 */
export function Stat(props: {
  readonly label: string;
  readonly value: string;
  readonly unit?: string;
  readonly sub?: string;
}): JSX.Element {
  return (
    <div className="vs-stat">
      <Text variant="section">{props.label}</Text>
      <span className="vs-stat-value">
        <Text variant="metric">{props.value}</Text>
        {props.unit === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.unit}
          </Text>
        )}
      </span>
      {props.sub === undefined ? null : (
        <Text variant="small" tone="muted">
          {props.sub}
        </Text>
      )}
    </div>
  );
}

/** What an empty list shows: a title, a body, and an optional action. */
export function EmptyState(props: {
  readonly title: string;
  readonly body?: string;
  readonly action?: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-empty">
      <Text variant="heading">{props.title}</Text>
      {props.body === undefined ? null : <Text tone="muted">{props.body}</Text>}
      {props.action === undefined ? null : <div className="vs-empty-action">{props.action}</div>}
    </div>
  );
}
