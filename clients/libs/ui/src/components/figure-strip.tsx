import type {JSX} from 'react';
import {Meter} from './meter.js';
import {Text} from './text.js';

/** One figure of a `FigureStrip`: what it counts, its value, and a quiet line under it. */
export interface Figure {
  readonly label: string;
  readonly value: string;
  /** What the value is measured against or covers: a limit, a window, how many machines reported. */
  readonly detail?: string;
  /** The whole account, for the pointer. */
  readonly hint?: string;
  /** `warn` draws the value in the problem tone, `muted` for a figure nobody reported. */
  readonly tone?: 'default' | 'warn' | 'muted';
  /** How far along a limit the value is, 0 to 1, drawn as a thin meter under it. */
  readonly share?: number;
}

/**
 * A row of labelled figures in one bordered band, divided by hairlines: the page's summary, read
 * left to right. Every figure has the same slot width so a column of bands lines up; the band wraps
 * as a whole when the window is narrow. The figures are one size, in the metric style, so none
 * competes with the page's title; a figure nobody reported is passed as a muted dash by
 * the caller, never as a zero.
 */
export function FigureStrip(props: {
  readonly label: string;
  readonly figures: readonly Figure[];
}): JSX.Element {
  return (
    <section className="vs-figure-strip" aria-label={props.label}>
      {props.figures.map(figure => (
        <div key={figure.label} className="vs-figure" title={figure.hint}>
          <Text variant="section">{figure.label}</Text>
          <span className="vs-figure-value">
            <Text
              variant="metric"
              tone={figure.tone === 'warn' ? 'warn' : figure.tone === 'muted' ? 'muted' : 'default'}
            >
              {figure.value}
            </Text>
          </span>
          {figure.share === undefined ? null : (
            <Meter
              decorative
              label={`${figure.label} ${(figure.share * 100).toFixed(0)}%`}
              value={figure.share}
              tone={figure.tone === 'warn' ? 'warn' : 'accent'}
            />
          )}
          {figure.detail === undefined ? null : (
            <span className="vs-figure-detail">
              <Text variant="caption" tone="muted">
                {figure.detail}
              </Text>
            </span>
          )}
        </div>
      ))}
    </section>
  );
}
