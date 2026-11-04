import {type JSX, type ReactNode, useId} from 'react';
import {Text} from './text.js';

/**
 * A flat panel. With `section` it is a titled region headed by that label in the `section` style;
 * `flush` drops the padding, for rows that reach its edges.
 */
export function Card(props: {
  readonly section?: string;
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
  return (
    <section className="vs-card" data-flush={flush} aria-labelledby={titleId}>
      <h2 className="vs-card-title" id={titleId}>
        <Text variant="section">{props.section}</Text>
      </h2>
      {props.children}
    </section>
  );
}
