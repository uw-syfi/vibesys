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

/** A key number: a `section` label, the value in the `metric` style, and an optional sub-label. */
export function Stat(props: {
  readonly label: string;
  readonly value: string;
  readonly sub?: string;
}): JSX.Element {
  return (
    <div className="vs-stat">
      <Text variant="section">{props.label}</Text>
      <Text variant="metric">{props.value}</Text>
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
