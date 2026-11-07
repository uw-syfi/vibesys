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
  readonly flush?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const titleId = useId();
  const flush = props.flush === true || undefined;
  if (props.section === undefined) {
    return (
      <div className="vs-card" data-flush={flush}>
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
    <section className="vs-card" data-flush={flush} aria-labelledby={titleId}>
      {props.action === undefined ? (
        title
      ) : (
        <div className="vs-card-head">
          {title}
          {props.action}
        </div>
      )}
      {props.children}
    </section>
  );
}
