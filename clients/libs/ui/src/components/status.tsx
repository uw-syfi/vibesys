import type {JSX} from 'react';
import {withoutPaths} from './paths.js';

/**
 * The tones a status reads in. `live` is in progress; it differs from `ok` by the ring of its dot
 * and by its label, never by color alone (one theme draws both in the same green).
 */
export type StatusTone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

/** A status dot, named by `label` for assistive technology; `live` adds a ring. */
export function StatusDot(props: {readonly tone: StatusTone; readonly label: string}): JSX.Element {
  return <span className="vs-dot" data-tone={props.tone} role="img" aria-label={props.label} />;
}

/** A rounded label in a tone, optionally led by a dot of the same tone. */
export function Chip(props: {
  readonly label: string;
  readonly tone?: StatusTone;
  readonly dot?: boolean;
  /** The label is an identifier a person may copy (a model id), set in the code face. */
  readonly mono?: boolean;
  /** The longer account behind a short label, for the pointer; a filesystem path in it is shown by its last name only (`withoutPaths`). */
  readonly hint?: string;
}): JSX.Element {
  const tone = props.tone ?? 'neutral';
  return (
    <span
      className="vs-chip"
      data-tone={tone}
      data-mono-id={props.mono === true || undefined}
      title={props.hint === undefined ? undefined : withoutPaths(props.hint)}
    >
      {props.dot === true ? <span className="vs-dot" data-tone={tone} aria-hidden="true" /> : null}
      {props.label}
    </span>
  );
}
