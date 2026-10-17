/** The words of the screen an ended run shows while its history is read (`historyScreen`). */

import type {HistoryProgress} from './run-page.js';

export interface HistoryLoadingView {
  readonly title: string;
  /** How far the read has come, in figures that carry their units. */
  readonly detail: string;
  /** The share read (0 to 1) when the run's total is known; null: no bar, only the figures. */
  readonly share: number | null;
}

const COUNT = new Intl.NumberFormat('en-US');

/** A size as people read it: `640 KB`, `8.0 MB` (decimal units, as the host's journal is sized). */
export function bytesWords(bytes: number): string {
  if (bytes < 1000) return `${COUNT.format(bytes)} B`;
  if (bytes < 1_000_000) return `${COUNT.format(Math.round(bytes / 1000))} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

/**
 * What the loading screen says: a determinate "n of N events" once the total is known, else the
 * events and bytes read so far with no bar, since nothing says how much is left.
 */
export function historyLoadingView(progress: HistoryProgress): HistoryLoadingView {
  const title = 'Reading the run’s history…';
  const size = progress.bytes > 0 ? ` · ${bytesWords(progress.bytes)} read` : '';
  const {events, total} = progress;
  if (total !== null && total > 0) {
    const shown = Math.min(events, total);
    return {
      title,
      detail: `${COUNT.format(shown)} of ${COUNT.format(total)} events${size}`,
      share: shown / total,
    };
  }
  if (events === 0) return {title, detail: 'Waiting for the first events', share: null};
  return {title, detail: `${COUNT.format(events)} events${size} so far`, share: null};
}
