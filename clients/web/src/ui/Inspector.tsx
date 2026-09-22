import type {InspectorModel} from '../model.js';

export interface InspectorProps {
  model: InspectorModel | null;
  /** >= 1024 px: an aside. 768-1023 px: a right drawer. < 768 px: a bottom sheet. */
  mode: 'aside' | 'drawer' | 'sheet';
  /** Drawer and sheet only. */
  open: boolean;
  designError: string | null;
  onClose: () => void;
  onRetryDesign: () => void;
}

// Scaffold; task 8 renders the inspector.
export function Inspector({mode}: InspectorProps) {
  return mode === 'aside' ? <aside className="insp" /> : null;
}
