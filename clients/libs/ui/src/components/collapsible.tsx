import {type JSX, type ReactNode, useId} from 'react';
import {Icon} from './icon.js';

/**
 * A titled block the reader opens and closes. The caller owns whether it is open (`open`) and hears
 * the press (`toggle`), so a list that unmounts blocks as they scroll away can keep them open.
 * `summary` is a muted one-line preview beside the title, which the caller shows while it is closed.
 */
export function Collapsible(props: {
  readonly label: string;
  readonly summary?: string;
  /** Muted text at the end of the title line, such as a size or a count. */
  readonly meta?: string;
  /** Something before the title that says what the block is: a status dot or a chip. */
  readonly leading?: ReactNode;
  /** Set the title in the mono face, for a name that is code. */
  readonly mono?: boolean;
  readonly open: boolean;
  readonly toggle: (open: boolean) => void;
  readonly children: ReactNode;
}): JSX.Element {
  const bodyId = useId();
  return (
    <div className="vs-collapsible">
      <button
        type="button"
        className="vs-collapsible-head"
        title={props.summary === undefined ? props.label : `${props.label}\n${props.summary}`}
        aria-expanded={props.open}
        aria-controls={bodyId}
        onClick={() => props.toggle(!props.open)}
      >
        <span className="vs-tree-chevron" data-open={props.open || undefined}>
          <Icon name="chevron-right" />
        </span>
        {props.leading}
        <span
          className="vs-collapsible-label"
          data-mono={props.mono === true || undefined}
          data-mono-id={props.mono === true || undefined}
        >
          {props.label}
        </span>
        {props.summary === undefined ? null : (
          <span className="vs-collapsible-summary">{props.summary}</span>
        )}
        {props.meta === undefined ? null : (
          <span className="vs-collapsible-meta">{props.meta}</span>
        )}
      </button>
      {props.open ? (
        <div className="vs-collapsible-body" id={bodyId}>
          {props.children}
        </div>
      ) : null}
    </div>
  );
}
