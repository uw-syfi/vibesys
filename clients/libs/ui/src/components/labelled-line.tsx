import type {JSX, ReactNode} from 'react';
import {Text} from './text.js';

/** One labelled part of a `LabelledLine`, set in a column of its own. */
export interface LabelledField {
  readonly label: string;
  readonly text: ReactNode;
}

/**
 * A short account as one row, not a card: a column for its `label` (and, under it, a quiet `note`
 * and a `badge`), then its `lead` (the one sentence that matters) and, under that, its `fields`
 * each under its own small label, in up to three columns. With no `fields` the row is as tall as its
 * lead, so an account that has not been written yet is one line.
 */
export function LabelledLine(props: {
  readonly label: string;
  readonly note?: string;
  readonly badge?: ReactNode;
  readonly lead: ReactNode;
  readonly fields?: readonly LabelledField[];
}): JSX.Element {
  const fields = props.fields ?? [];
  return (
    <section className="vs-labelled-line" aria-label={props.label}>
      <div className="vs-labelled-line-label">
        <Text variant="section">{props.label}</Text>
        {props.note === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.note}
          </Text>
        )}
        {props.badge}
      </div>
      <div className="vs-labelled-line-body">
        <div className="vs-labelled-line-lead">{props.lead}</div>
        {fields.length === 0 ? null : (
          <dl className="vs-labelled-line-fields">
            {fields.map(field => (
              <div key={field.label} className="vs-labelled-line-field">
                <dt>
                  <Text variant="section">{field.label}</Text>
                </dt>
                <dd>
                  <Text variant="small" tone="muted">
                    {field.text}
                  </Text>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </section>
  );
}
