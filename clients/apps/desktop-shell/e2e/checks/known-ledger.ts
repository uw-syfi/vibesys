/**
 * `KnownLedger`: what one screen suite saw of the known failures. Each suite's screens are walked
 * in its own spec, so each suite says which of its entries still happen, and fails on one that no
 * screen shows any more.
 *
 * The screens of a spec run on any worker, in any order, so no worker sees them all. A ledger that
 * owns its suite (`requireEveryFailure`) therefore writes what each screen matched to a file of its
 * own, and the check that a fixed defect leaves the list (`known-failures-final.ts`) runs after
 * every screen test of the run and reads the files of every worker.
 */
import {createHash} from 'node:crypto';
import {mkdirSync, readdirSync, readFileSync, writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import type {Violation} from './in-page.js';
import {KNOWN_FAILURES, type KnownFailure, type Suite} from './known-failures.js';
import {report} from './screen-check.js';

/**
 * Where the owners record what their screens matched. Playwright empties its output directory when
 * a run starts, so a run reads only what its own screens wrote.
 */
export const LEDGER_DIRECTORY = fileURLToPath(
  new URL('../../test-results/known-ledger/', import.meta.url),
);

/** The entries of `failures` that `matched` (the ids some screen showed) does not name. */
export function staleFailures(
  failures: readonly KnownFailure[],
  matched: ReadonlySet<string>,
): string[] {
  return failures.filter(each => !matched.has(each.id)).map(each => each.id);
}

/** The suites that have an entry, each with its entries. */
export function suitesWithFailures(
  failures: readonly KnownFailure[] = KNOWN_FAILURES,
): ReadonlyMap<Suite, readonly KnownFailure[]> {
  const suites = new Map<Suite, KnownFailure[]>();
  for (const each of failures) suites.set(each.suite, [...(suites.get(each.suite) ?? []), each]);
  return suites;
}

/**
 * The entry ids the screens of `suite` matched in this run, from the files its owner wrote, or
 * `null` when none was written: no spec of the suite ran in this slice of the suite.
 */
export function recordedMatches(
  suite: Suite,
  directory: string = LEDGER_DIRECTORY,
): ReadonlySet<string> | null {
  let files: string[];
  try {
    files = readdirSync(`${directory}/${suite}`);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw error;
  }
  const matched = new Set<string>();
  for (const file of files) {
    const record: unknown = JSON.parse(readFileSync(`${directory}/${suite}/${file}`, 'utf8'));
    for (const id of (record as {matched: string[]}).matched) matched.add(id);
  }
  return files.length === 0 ? null : matched;
}

export class KnownLedger {
  readonly #suite: Suite;
  readonly #failures: readonly KnownFailure[];
  readonly #directory: string;
  readonly #matched = new Set<string>();
  readonly #screens = new Set<string>();
  #owner = false;

  constructor(
    suite: Suite,
    failures: readonly KnownFailure[] = KNOWN_FAILURES,
    directory: string = LEDGER_DIRECTORY,
  ) {
    this.#suite = suite;
    this.#failures = failures.filter(each => each.suite === suite);
    this.#directory = directory;
  }

  /** How many screens this worker has looked at. */
  get screens(): number {
    return this.#screens.size;
  }

  /** The violations of screen `name` no known failure explains, as lines for a failure message. */
  unexplained(name: string, violations: readonly Violation[]): string[] {
    this.#screens.add(name);
    const result = report(name, violations, this.#failures);
    const matched = result.known.map(each => each.failure.id);
    for (const id of matched) this.#matched.add(id);
    if (this.#owner) this.#record(name, matched);
    return result.unexplained.map(
      each => `${name}: [${each.check}] ${each.message} (${each.where})`,
    );
  }

  /** The entries of this suite no screen this worker walked has shown. */
  stale(): string[] {
    return staleFailures(this.#failures, this.#matched);
  }

  /**
   * Makes this spec the owner of its suite: what each of its screens matches is recorded for the
   * check that a fixed defect leaves the list. Call it once, at the end of the spec.
   */
  requireEveryFailure(): void {
    this.#owner = true;
  }

  #record(name: string, matched: readonly string[]): void {
    const directory = `${this.#directory}/${this.#suite}`;
    mkdirSync(directory, {recursive: true});
    const file = `${createHash('sha1').update(name).digest('hex')}.json`;
    writeFileSync(`${directory}/${file}`, JSON.stringify({screen: name, matched}));
  }
}
