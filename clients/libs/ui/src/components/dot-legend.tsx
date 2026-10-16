import type {JSX} from 'react';
import {StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** A one-line key to status dots: each tone, its word and, if the caller adds one, a count. */
export function DotLegend(props: {
  readonly items: readonly {readonly tone: StatusTone; readonly label: string}[];
}): JSX.Element {
  return (
    <span className="vs-dotlegend">
      {props.items.map(item => (
        <span key={item.label} className="vs-dotlegend-item">
          <StatusDot tone={item.tone} label={item.label} />
          <Text variant="small" tone="muted">
            {item.label}
          </Text>
        </span>
      ))}
    </span>
  );
}
