import type {JSX} from 'react';
import {withoutPaths} from './paths.js';

/**
 * The tones a status reads in. `alt` is a fifth color, apart from the accent, for a state that is none of the others (set aside). `live` is in progress; it differs from `ok` by the ring of its dot
 * and by its label, never by color alone (one theme draws both in the same green).
 */
export type StatusTone = 'neutral' | 'live' | 'ok' | 'warn' | 'err' | 'alt';

/** A status dot, named by `label` for assistive technology; `live` adds a ring. */
export function StatusDot(props: {readonly tone: StatusTone; readonly label: string}): JSX.Element {
  return <span className="vs-dot" data-tone={props.tone} role="img" aria-label={props.label} />;
}

/**
 * A rounded label in a tone, optionally led by a dot of the same tone. A chip with a `hint` takes
 * the keyboard focus and exposes the hint as its accessible description; the hint is drawn under
 * the chip on hover and on focus (`.vs-chip[aria-description]`), so no one needs a pointer to read it.
 */
export function Chip(props: {
  readonly label: string;
  readonly tone?: StatusTone;
  readonly dot?: boolean;
  /** The label is an identifier a person may copy (a model id), set in the code face. */
  readonly mono?: boolean;
  /**
   * The label is a name a person chose and may be long: it is cut with an ellipsis (its whole text
   * on hover) when the chip's room is less than it needs, instead of pushing its row wider.
   */
  readonly clip?: boolean;
  /** One of a column of chips that are alike: it is as wide as the longest of them, whatever its label. */
  readonly even?: boolean;
  /** The longer account behind a short label; a filesystem path in it is shown by its last name only (`withoutPaths`). */
  readonly hint?: string;
}): JSX.Element {
  const tone = props.tone ?? 'neutral';
  const hint = props.hint === undefined ? undefined : withoutPaths(props.hint);
  return (
    <span
      className="vs-chip"
      data-tone={tone}
      data-mono-id={props.mono === true || undefined}
      data-clip={props.clip === true || undefined}
      data-even={props.even === true || undefined}
      title={props.clip === true && hint === undefined ? props.label : undefined}
      aria-description={hint}
      // The hint is information a keyboard and a screen reader must reach (WCAG 1.4.13, 2.1.1), which
      // a `title` is not: so a chip with one is a tab stop. It is no button (it has no action), and a
      // description node inside it would put the hint into the chip's text.
      tabIndex={hint === undefined ? undefined : 0}
    >
      {props.dot === true ? <span className="vs-dot" data-tone={tone} aria-hidden="true" /> : null}
      {props.clip === true ? <span className="vs-chip-label">{props.label}</span> : props.label}
    </span>
  );
}
