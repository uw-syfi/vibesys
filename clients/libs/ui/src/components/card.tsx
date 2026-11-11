import {type JSX, type ReactNode, useId} from 'react';
import {Text} from './text.js';

/**
 * A flat panel. With `section` it is a titled region headed by that label in the `section` style,
 * and an `action` (the link that opens what the card summarises) sits at the end of the heading's
 * line, so every card's link is in the same place on the same baseline; `flush` drops the padding,
 * for rows that reach its edges.
 */
export function Card(props: {
  readonly section?: string;
  readonly action?: ReactNode;
  /** What stands right after the heading, left-aligned with it (a key to what the card draws). */
  readonly beside?: ReactNode;
  readonly flush?: boolean;
  /** Less padding and a closer gap, for a page that packs many cards; the default is for the rest. */
  readonly dense?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const titleId = useId();
  const flush = props.flush === true || undefined;
  const dense = props.dense === true || undefined;
  if (props.section === undefined) {
    return (
      <div className="vs-card" data-flush={flush} data-dense={dense}>
        {props.children}
      </div>
    );
  }
  const title = (
    <h2 className="vs-card-title" id={titleId}>
      <Text variant="section">{props.section}</Text>
    </h2>
  );
  return (
    <section className="vs-card" data-flush={flush} data-dense={dense} aria-labelledby={titleId}>
      {props.action === undefined && props.beside === undefined ? (
        title
      ) : (
        <div className="vs-card-head">
          {title}
          {props.beside === undefined ? null : <div className="vs-card-beside">{props.beside}</div>}
          {props.action === undefined ? null : <div className="vs-card-action">{props.action}</div>}
        </div>
      )}
      {props.children}
    </section>
  );
}
