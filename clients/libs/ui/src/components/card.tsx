import {type JSX, type ReactNode, useId} from 'react';
import {Text} from './text.js';

export type BannerTone = 'neutral' | 'ok' | 'warn' | 'err';

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

/**
 * A message in a tone: a title, an optional body, and optional actions beside them. `hint` is the
 * longer account behind a short title or body, for the pointer.
 */
export function Banner(props: {
  readonly title: string;
  readonly tone?: BannerTone;
  readonly body?: ReactNode;
  readonly hint?: string;
  readonly actions?: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-banner" data-tone={props.tone ?? 'neutral'}>
      <div className="vs-banner-text" title={props.hint}>
        <Text variant="body-strong">{props.title}</Text>
        {props.body === undefined ? null : (
          <div>
            <Text tone="muted">{props.body}</Text>
          </div>
        )}
      </div>
      {props.actions === undefined ? null : (
        <div className="vs-banner-actions">{props.actions}</div>
      )}
    </div>
  );
}
