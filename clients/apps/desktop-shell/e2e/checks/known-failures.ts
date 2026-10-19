/**
 * The screen defects the checks found on main that no test change fixes: each is listed here with
 * the issue that tracks it, so the checks pass today and fail on anything new. A check is never
 * skipped; a known failure names the check, the text it matches and the screens it is seen on. The
 * list is a debt: `screens.spec.ts` fails when an entry matches nothing, so fixing a defect means
 * deleting its entry in the same change.
 *
 * Screens are named `<fixture>/<screen>/<width>` (plus `/paper` in the
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

export const KNOWN_FAILURES: readonly KnownFailure[] = [];

export function isKnown(failure: KnownFailure, screen: string, violation: Violation): boolean {
  return (
    failure.check === violation.check &&
    failure.message.test(violation.message) &&
    (failure.screens?.test(screen) ?? true)
  );
}
