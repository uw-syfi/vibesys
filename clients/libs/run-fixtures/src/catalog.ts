/**
 * The committed fixtures (`data/*.json`), validated on first use, and the one fixture too large to
 * commit. `pnpm --filter @vibesys/app-desktop-shell fixtures` rewrites the data files; a test holds
 * the synthetic ones equal to their scenarios.
 */
import continuation from './data/continuation-rounds.json' with {type: 'json'};
import failures from './data/failure-reasons.json' with {type: 'json'};
import finished from './data/finished-run.json' with {type: 'json'};
import reconnect from './data/live-reconnect.json' with {type: 'json'};
import longTitles from './data/long-titles.json' with {type: 'json'};
import bases from './data/non-input-bases.json' with {type: 'json'};
import queue from './data/queue-rs-live.json' with {type: 'json'};
import retried from './data/retries.json' with {type: 'json'};
import {parseFixture, type RunFixture} from './fixture.js';
import {weeksRun} from './weeks.js';

const SOURCES: readonly unknown[] = [
  queue,
  longTitles,
  failures,
  retried,
  continuation,
  bases,
  reconnect,
  finished,
];

let parsed: readonly RunFixture[] | null = null;

/** Every committed fixture, validated. */
export function committedFixtures(): readonly RunFixture[] {
  parsed ??= SOURCES.map(parseFixture);
  return parsed;
}

/** The names of every fixture `fixtureNamed` knows: the committed ones, and the 14-day run. */
export const FIXTURE_NAMES: readonly string[] = [
  'queue-rs-live',
  'long-titles',
  'failure-reasons',
  'retries',
  'continuation-rounds',
  'non-input-bases',
  'live-reconnect',
  'finished-run',
  'weeks-600-ideas',
];

/** The fixture called `name`; throws naming the known names when there is none. */
export function fixtureNamed(name: string): RunFixture {
  if (name === 'weeks-600-ideas') return weeksRun();
  const found = committedFixtures().find(fixture => fixture.name === name);
  if (found === undefined) {
    throw new Error(`no fixture named ${name}; known: ${FIXTURE_NAMES.join(', ')}`);
  }
  return found;
}
