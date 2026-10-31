import {type JSX, type ReactNode, useId, useState} from 'react';
import {DetailsToggle} from './details-toggle.js';
import {Text} from './text.js';

/**
 * The tones of a banner, which describes a state its screen is in. `failed` is a lasting failure
 * the screen holds, such as a job that ended in failure; a failure that is an event the user passes
 * through ("could not save") is reported as a problem and shown as a toast, so there is no `err`.
 */
export type BannerTone = 'neutral' | 'ok' | 'warn' | 'failed';

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
 * A state its screen is in, on one line: the title, with the actions beside it. The longer account
 * (`body`, then `hint`) is behind a details control at the end of the line; a banner with neither
 * has nothing to open.
 */
export function Banner(props: {
  readonly title: string;
  readonly tone?: BannerTone;
  readonly body?: ReactNode;
  readonly hint?: string;
  readonly actions?: ReactNode;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const detailed = props.body !== undefined || props.hint !== undefined;
  return (
    <div className="vs-banner" data-tone={props.tone ?? 'neutral'}>
      <div className="vs-banner-text">
        <span className="vs-banner-title">
          <Text variant="body-strong">{props.title}</Text>
        </span>
        {detailed && open ? (
          <>
            {props.body === undefined ? null : <Text tone="muted">{props.body}</Text>}
            {props.hint === undefined ? null : <Text tone="muted">{props.hint}</Text>}
          </>
        ) : null}
      </div>
      {props.actions === undefined && !detailed ? null : (
        <div className="vs-banner-actions">
          {props.actions}
          {detailed ? <DetailsToggle open={open} toggle={setOpen} /> : null}
        </div>
      )}
    </div>
  );
}
