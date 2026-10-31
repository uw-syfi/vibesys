import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {RunEvent} from '@vibesys/contracts';
import {EMPTY_TRACE} from '@vibesys/run-state/trace';
import {heroView} from './headline.js';
import {traceLog} from './live.js';
import {createRunReader} from './run-model.js';

/**
 * The sidebar's row for a run (the host helper's digest: `ideas`, `adopted_ratio` and `best_so_far_ratio`) and the run
 * page it opens must say the same of the run. The fixtures under
 * `tests/conformance/fixtures/run-digest` hold a journal and the two figures the digest folds from
 * it (`tests/server/test_run_digest_fixtures.py`); this test reads the same journal as the page
 * does (its idea count and its headline tile), so the two cannot drift apart.
 */

const FIXTURES = resolve(import.meta.dir, '../../../../../tests/conformance/fixtures/run-digest');

interface Fixture {
  readonly events: readonly RunEvent[];
  readonly digest: {
    readonly ideas: number;
    readonly adopted_ratio: number | null;
    readonly best_so_far_ratio?: number | null;
  };
}

const fixtures: Array<{name: string; fixture: Fixture}> = readdirSync(FIXTURES)
  .filter(name => name.endsWith('.json'))
  .sort()
  .map(name => ({
    name,
    fixture: JSON.parse(readFileSync(resolve(FIXTURES, name), 'utf8')) as Fixture,
  }));

describe('the digest of a run says what its page says', () => {
  for (const {name, fixture} of fixtures) {
    test(`${name}: the ideas and the headline ratio`, () => {
      const model = createRunReader().read(traceLog(EMPTY_TRACE, null, fixture.events), null);

      const headline = heroView(model).headlines[0]?.ratio ?? null;

      expect(model.candidates.candidates.length).toBe(fixture.digest.ideas);
      // The headline is the adopted result, or while the run works the best gate so far.
      const figure = fixture.digest.adopted_ratio ?? fixture.digest.best_so_far_ratio ?? null;
      expect(headline).toBe(figure === null ? null : `${figure.toFixed(2)}×`);
    });
  }

  test('the corpus holds a fixture', () => {
    expect(fixtures.length).toBeGreaterThan(0);
  });
});
