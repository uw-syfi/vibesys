/**
 * How an idea's verdict is written and drawn: one mapping for every place a verdict shows (the filter
 * chips and the table's chips, the workstreams' bars and their key), so one color never means two
 * verdicts.
 */
import type {StatusTone} from '@vibesys/ui';
import type {Verdict} from '../../../state/index.js';

export const VERDICT_WORDS = {
  open: 'Open',
  kept: 'Kept',
  rejected: 'Rejected',
  parked: 'Parked',
} as const satisfies Record<Verdict, string>;

export const VERDICT_TONES = {
  open: 'live',
  kept: 'ok',
  rejected: 'neutral',
  parked: 'alt',
} as const satisfies Record<Verdict, StatusTone>;
