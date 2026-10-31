/**
 * `KnownLedger`: what one screen suite saw of the known failures. Each suite's screens are walked
 * in its own spec, so each suite says which of its entries still happen, and fails on one that no
 * screen shows any more.
 */
import {expect, test} from '@playwright/test';
import type {Violation} from './in-page.js';
import {KNOWN_FAILURES, type KnownFailure, type Suite} from './known-failures.js';
import {report} from './screen-check.js';

export class KnownLedger {
  readonly #failures: readonly KnownFailure[];
  readonly #matched = new Set<string>();
  readonly #screens = new Set<string>();

  constructor(suite: Suite, failures: readonly KnownFailure[] = KNOWN_FAILURES) {
    this.#failures = failures.filter(each => each.suite === suite);
  }

  /** How many screens this run has looked at. */
  get screens(): number {
    return this.#screens.size;
  }

  /** The violations of screen `name` no known failure explains, as lines for a failure message. */
  unexplained(name: string, violations: readonly Violation[]): string[] {
    this.#screens.add(name);
    const result = report(name, violations, this.#failures);
    for (const each of result.known) this.#matched.add(each.failure.id);
    return result.unexplained.map(
      each => `${name}: [${each.check}] ${each.message} (${each.where})`,
    );
  }

  /** The entries of this suite no screen has shown. */
  stale(): string[] {
    return this.#failures.filter(each => !this.#matched.has(each.id)).map(each => each.id);
  }

  /**
   * Adds the test that a fixed defect leaves the list; last in its spec. Only the whole suite can
   * say a failure is gone, so a filtered run, for which `complete` is false, skips it.
   */
  requireEveryFailure(complete: () => boolean): void {
    test('every known failure still happens, so a fixed defect leaves the list', () => {
      test.skip(!complete(), 'a filtered run: not every screen was walked');
      const stale = this.stale();
      expect(
        stale,
        `known failures no screen shows any more, delete them: ${stale.join(', ')}`,
      ).toEqual([]);
    });
  }
}
