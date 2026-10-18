import type {JSX} from 'react';
import {StatusDot, type StatusTone} from './status.js';
import {Text} from './text.js';

/** One entry of a `DotLegend`; with `toggle` it is a filter chip and `on` says whether its rows show. */
export interface LegendItem {
  readonly tone: StatusTone;
  readonly label: string;
  readonly on?: boolean;
  readonly toggle?: (on: boolean) => void;
  /** A chip with nothing behind it (a count of zero) cannot be pressed. */
  readonly disabled?: boolean;
}

/**
 * A one-line key to status dots: each tone and its word (a count too, if the caller adds one).
 * An item with `toggle` is a button that shows or hides the rows of its state (`aria-pressed`).
 */
export function DotLegend(props: {readonly items: readonly LegendItem[]}): JSX.Element {
  return (
    <span className="vs-dotlegend">
      {props.items.map(item => {
        const toggle = item.toggle;
        const face = (
          <>
            <StatusDot tone={item.tone} label={item.label} />
            <Text variant="small" tone="muted">
              {item.label}
            </Text>
          </>
        );
        return toggle === undefined ? (
          <span key={item.label} className="vs-dotlegend-item" data-tone={item.tone}>
            {face}
          </span>
        ) : (
          <button
            key={item.label}
            type="button"
            className="vs-dotlegend-item"
            data-tone={item.tone}
            aria-pressed={item.on === true}
            disabled={item.disabled === true}
            onClick={() => toggle(item.on !== true)}
          >
            {face}
          </button>
        );
      })}
    </span>
  );
}
