import type {InspectorModel} from '../model.js';

export interface InspectorProps {
  model: InspectorModel | null;
  /** >= 1024 px: an aside. 768-1023 px: a right drawer. < 768 px: a bottom sheet. */
  mode: 'aside' | 'drawer' | 'sheet';
  /** Drawer and sheet only. */
  open: boolean;
  /**
   * True only while the selected round's backfill is in flight: its verdicts are not loaded yet,
   * so the Judge section renders skeleton rows with `aria-busy="true"`. False after a failed
   * backfill; the Judge section then follows its normal empty rule, and the log carries the
   * error and Retry.
   */
  judgePending: boolean;
  designError: string | null;
  onClose: () => void;
  onRetryDesign: () => void;
}

// Scaffold; task 8 renders the inspector.
export function Inspector({mode}: InspectorProps) {
  return mode === 'aside' ? <aside className="insp" /> : null;
}
