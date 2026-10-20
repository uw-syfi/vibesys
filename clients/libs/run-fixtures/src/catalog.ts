/**
 * The committed fixtures (`data/*.json`), validated on first use, and the one fixture too large to
 * commit. `pnpm --filter @vibesys/app-desktop-shell fixtures` rewrites the data files; a test holds
 * the synthetic ones equal to their scenarios.
 */
import basesAndEffects from './data/bases-and-effects.json' with {type: 'json'};
import comparisonBaselines from './data/comparison-baselines.json' with {type: 'json'};
import continuation from './data/continuation-rounds.json' with {type: 'json'};
import crashed from './data/crashed-after-turn.json' with {type: 'json'};
import failures from './data/failure-reasons.json' with {type: 'json'};
import finished from './data/finished-run.json' with {type: 'json'};
import tenIdeas from './data/finished-ten-ideas.json' with {type: 'json'};
import twoObjectives from './data/finished-two-objectives.json' with {type: 'json'};
import gateFailed from './data/input-gate-failed.json' with {type: 'json'};
import reconnect from './data/live-reconnect.json' with {type: 'json'};
import longTitles from './data/long-titles.json' with {type: 'json'};
import bases from './data/non-input-bases.json' with {type: 'json'};
import queue from './data/queue-rs-live.json' with {type: 'json'};
import quotaPause from './data/quota-pause.json' with {type: 'json'};
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
  basesAndEffects,
  reconnect,
  finished,
  tenIdeas,
  twoObjectives,
  quotaPause,
  gateFailed,
  comparisonBaselines,
  crashed,
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
  'bases-and-effects',
  'live-reconnect',
  'finished-run',
  'finished-ten-ideas',
  'finished-two-objectives',
  'quota-pause',
  'input-gate-failed',
  'comparison-baselines',
  'crashed-after-turn',
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
