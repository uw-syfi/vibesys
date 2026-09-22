import type {RailModel} from '../model.js';

export interface RailProps {
  state: 'loading' | 'unattached' | 'ready';
  model: RailModel;
  selected: number | null;
  /** The experiments query failed; shown under the rows with Retry. */
  error: string | null;
  /** Phone only, until the first sheet opens. */
  hint: boolean;
  onSelect: (round: number) => void;
  onRetry: () => void;
}

// Scaffold; task 6 renders the rail.
export function Rail(_props: RailProps) {
  return <nav className="rail" aria-label="Rounds" />;
}
