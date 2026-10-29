import {type JSX, type ReactNode, useId} from 'react';
import {type CheckState, STATES} from './check-list.js';
import {Icon} from './icon.js';

/**
 * A panel for a page's primary action, to be pinned in a `Page` footer: a one-line status that
 * opens into its details, a summary of what the action will do, and the action itself. The status
 * is a button that shows and hides `details`; the caller owns whether they are `open`, so it can
 * open them by itself when something needs the reader (a failed check). `details` scroll within
 * the panel when tall, so the action never leaves the window.
 */
export function ActionPanel(props: {
  /** The panel's accessible name. */
  readonly label: string;
  readonly status: {readonly state: CheckState; readonly text: string};
  readonly details: ReactNode;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
  /** What the action will do, in a line. */
  readonly summary?: string;
  readonly action: ReactNode;
}): JSX.Element {
  const detailsId = useId();
  const state = STATES[props.status.state];
  return (
    <section className="vs-action-panel" aria-label={props.label}>
      {props.open ? (
        <section
          className="vs-action-panel-details"
          id={detailsId}
          aria-label="Details"
          // biome-ignore lint/a11y/noNoninteractiveTabindex: a box that scrolls must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls the details.
          tabIndex={0}
        >
          {props.details}
        </section>
      ) : null}
      <div className="vs-action-panel-bar">
        <button
          type="button"
          className="vs-action-panel-status"
          data-state={props.status.state}
          aria-expanded={props.open}
          aria-controls={detailsId}
          onClick={() => props.toggle(!props.open)}
        >
          <span className="vs-action-panel-icon">
            <Icon name={state.icon} label={state.word} />
          </span>
          <span>{props.status.text}</span>
          <span className="vs-action-panel-chevron" data-open={props.open || undefined}>
            <Icon name="chevron-down" />
          </span>
        </button>
        {props.summary === undefined ? null : (
          <p className="vs-action-panel-summary">{props.summary}</p>
        )}
        <div className="vs-action-panel-action">{props.action}</div>
      </div>
    </section>
  );
}
