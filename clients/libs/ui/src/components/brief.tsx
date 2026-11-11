import {type JSX, type ReactNode, useCallback, useState} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {Text} from './text.js';

/** One part of a `Brief`: a small label over its text, cut at three lines. */
export interface BriefField {
  readonly label: string;
  readonly text: ReactNode;
  /** The whole text as plain words, which the pointer shows when the text is cut. */
  readonly title: string;
}

/** What a clamped block tells its brief: whether the text is cut now. */
type Report = (cut: boolean) => void;

/**
 * Text cut to `lines` lines with an ellipsis, and whole on hover only when it is cut. Whether it is
 * cut is measured on the drawn box (its content is taller than the box), as the browser sets it
 * and again whenever the box changes size, and told to the brief, so a link to the whole text
 * appears exactly when something is hidden. A long word breaks inside itself and never overflows.
 */
function Clamped(props: {
  readonly lines: number;
  readonly kind: 'lead' | 'field';
  readonly title: string;
  readonly cut: boolean;
  readonly report: Report;
  readonly children: ReactNode;
}): JSX.Element {
  const {report} = props;
  const measure = useCallback(
    (element: HTMLElement | null) => {
      if (element === null || typeof ResizeObserver === 'undefined') return undefined;
      const read = (): void => report(element.scrollHeight > element.clientHeight + 1);
      read();
      const watch = new ResizeObserver(read);
      watch.observe(element);
      return () => watch.disconnect();
    },
    [report],
  );
  const style: StyleWithVars = {'--clamp-lines': props.lines};
  return (
    <p
      className="vs-brief-clamp"
      data-kind={props.kind}
      style={style}
      ref={measure}
      title={props.cut ? props.title : undefined}
    >
      {props.children}
    </p>
  );
}

/**
 * A short account written by someone else, as one block and not a card: a rule on its left, a small
 * quiet line saying whose it is and how old (`label`), the `lead` (one step above the body, cut at
 * two lines), then each field as a small label over its text (cut at three lines), the labels close
 * to their text and the rows apart. The block is as wide as a page of text reads well. When
 * anything is cut, `more` (the way to the whole text) appears under it. With `empty` there is no
 * account yet, and the block is that one quiet line. Nothing is bold: the text is of unknown length,
 * so it is never set large.
 */
export function Brief(props: {
  /** What the block is called to a screen reader and to a test: its region's name. Defaults to `label`. */
  readonly name?: string;
  readonly label?: string;
  readonly lead?: ReactNode;
  readonly leadTitle?: string;
  readonly fields?: readonly BriefField[];
  readonly more?: ReactNode;
  readonly empty?: string;
}): JSX.Element {
  const [cuts, setCuts] = useState<ReadonlySet<string>>(new Set());
  const reporter = useCallback(
    (key: string): Report =>
      cut =>
        setCuts(held => {
          if (held.has(key) === cut) return held;
          const next = new Set(held);
          if (cut) next.add(key);
          else next.delete(key);
          return next;
        }),
    [],
  );
  if (props.empty !== undefined) {
    return (
      <Text variant="small" tone="muted">
        {props.empty}
      </Text>
    );
  }
  const fields = props.fields ?? [];
  return (
    <section className="vs-brief" aria-label={props.name ?? props.label}>
      {props.label === undefined ? null : (
        <Text variant="caption" tone="muted">
          {props.label}
        </Text>
      )}
      {props.lead === undefined ? null : (
        <Clamped
          lines={2}
          kind="lead"
          title={props.leadTitle ?? ''}
          cut={cuts.has('lead')}
          report={reporter('lead')}
        >
          {props.lead}
        </Clamped>
      )}
      {fields.length === 0 ? null : (
        <div className="vs-brief-fields">
          {fields.map(field => (
            <div key={field.label} className="vs-brief-field">
              <Text variant="caption" tone="muted">
                {field.label}
              </Text>
              <Clamped
                lines={3}
                kind="field"
                title={field.title}
                cut={cuts.has(field.label)}
                report={reporter(field.label)}
              >
                {field.text}
              </Clamped>
            </div>
          ))}
        </div>
      )}
      {cuts.size === 0 || props.more === undefined ? null : (
        <div className="vs-brief-more">{props.more}</div>
      )}
    </section>
  );
}
