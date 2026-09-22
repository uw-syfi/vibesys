import type {LogGroup} from '../model.js';

export interface LogProps {
  state: 'loading' | 'ready';
  round: number | null;
  groups: LogGroup[];
  /** The selected round is the latest one: pin to the bottom until the reader scrolls up. */
  follow: boolean;
  /** Backfill below the tail floor, which App runs on its own; Retry only after an error. */
  history: {loading: boolean; error: string | null; onRetry: () => void};
}

// Scaffold; task 7 renders the log.
export function Log(_props: LogProps) {
  return <div className="logwrap" id="log" />;
}
