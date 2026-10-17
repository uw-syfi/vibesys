/**
 * The screen defects the checks found on main that no test change fixes: each is listed here with
 * the issue that tracks it, so the checks pass today and fail on anything new. A check is never
 * skipped; a known failure names the check, the text it matches and the screens it is seen on. The
 * list is a debt: `screens.spec.ts` fails when an entry matches nothing, so fixing a defect means
 * deleting its entry in the same change.
 *
 * Screens are named `<fixture>/<screen>/sidebar-<shown|hidden>/<width>` (plus `/paper` in the
 * light theme).
 */
import type {CheckId, Violation} from './in-page.js';

export interface KnownFailure {
  /** A short name, for the failure message of a stale entry. */
  readonly id: string;
  readonly check: CheckId;
  /** Matches the violation's message. */
  readonly message: RegExp;
  /** Matches the screen's name; every screen when absent. */
  readonly screens?: RegExp;
  /** The issue that tracks the fix. */
  readonly issue: string;
}

const ISSUE = 'https://github.com/uw-syfi/vibesys/issues/3063';
const NARROW = /\/1000$/;

export const KNOWN_FAILURES: readonly KnownFailure[] = [
  {id: 'truncated-without-tooltip', check: 'truncation', message: /./, issue: ISSUE},
  {
    id: 'raw-metric-id',
    check: 'raw-text',
    message: /snake_case identifier "total_ops_per_sec"/,
    issue: ISSUE,
  },
  {id: 'raw-markdown-in-title', check: 'raw-text', message: /raw markdown/, issue: ISSUE},
  {
    id: 'time-bar-labels-overlap',
    check: 'overlap',
    message: /"(Call \d+|Live|Back to live)" and "Call \d+"/,
    issue: ISSUE,
  },
  {
    id: 'timeline-ticks-overlap',
    check: 'overlap',
    message: /^text overlaps text: "[0-9hmsd ]+" and "[0-9hmsd ]+"$/,
    screens: NARROW,
    issue: ISSUE,
  },
  {
    id: 'best-line-overflows-its-pane',
    check: 'overflow',
    message: /a pane scrolls sideways/,
    screens: /^long-titles\//,
    issue: ISSUE,
  },
  {
    id: 'chart-moves-when-scrubbed',
    check: 'stability',
    message: /performance chart changed its y at "scrubbed"/,
    screens: NARROW,
    issue: ISSUE,
  },
];

export function isKnown(failure: KnownFailure, screen: string, violation: Violation): boolean {
  return (
    failure.check === violation.check &&
    failure.message.test(violation.message) &&
    (failure.screens?.test(screen) ?? true)
  );
}
