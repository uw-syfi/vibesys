/**
 * Three hosts the Cost page has to hold in one layout, as scripted worlds for the fixture page and the
 * tests: a host whose one run has not finished a turn (no spend yet), a host with one short run (nine
 * turns, four of them unpriced, all in a few hours), and a host with a week of runs on several models and
 * harnesses, some of whose agents are unpriced. Each is a function of nothing: the same bytes every time.
 * The cost the host serves for them is worked out by `servedHostCost`, like every other fixture's.
 */
import {Harness, oneofIs} from '@vibesys/orchestrator-proto';
import {type HostCostView, readHostCost} from '../../state/index.js';
import {fleetScenario} from './fleet.js';
import {generateScenario, type Scenario} from './scenario.js';
import {servedHostCost} from './served-host-cost.js';

/** The names a fixture address gives the three cases (`?spend=none`). */
export const COST_CASES = ['none', 'short', 'week'] as const;
export type CostCase = (typeof COST_CASES)[number];

/** The moment the week case is read at: a Monday midday, so the week's axis has its midnights. */
const WEEK_NOW_MS = Date.UTC(2026, 10, 9, 20, 0, 0);

/** The models the week's workers run on, in turn. */
const WEEK_WORKERS = [
  {harness: Harness.CLAUDE_CODE, model: 'claude-opus-5-5'},
  {harness: Harness.CLAUDE_CODE, model: 'claude-sonnet-5-5'},
  {harness: Harness.CODEX, model: 'gpt-5-codex'},
  {harness: Harness.CLAUDE_CODE, model: 'claude-haiku-5-5'},
] as const;

/** Every kind of change a run's log carries, so a kind is told apart through `oneofIs`. */
const CHANGE_KINDS = {
  run: true,
  agent: true,
  turn: true,
  item: true,
  message: true,
  receipt: true,
  checkIn: true,
  runUpdated: true,
} as const;

/** The run of the scenario and every agent of it, with none of its turns finished. */
function noTurns(scenario: Scenario): Scenario {
  return {
    ...scenario,
    // An item belongs to a turn, so it goes with them; the log's sequence has no gaps.
    changes: scenario.changes
      .filter(
        change =>
          !oneofIs(change.body, 'turn', CHANGE_KINDS) &&
          !oneofIs(change.body, 'item', CHANGE_KINDS),
      )
      .map((change, at) => ({...change, sequence: BigInt(at + 1)})),
  };
}

/** The world of a case: what the page's host holds, its runs and their turns. */
export function costCaseScenario(name: CostCase): Scenario {
  switch (name) {
    case 'none':
      return noTurns(generateScenario({seed: 5, subagents: 1, live: true}));
    case 'short':
      return generateScenario({seed: 5, subagents: 8, live: false});
    case 'week':
      return fleetScenario({
        seed: 21,
        prefix: 'h0',
        runs: 16,
        live: 2,
        nowMs: WEEK_NOW_MS,
        stepHours: 10,
        workers: WEEK_WORKERS,
      });
  }
}

/** The host cost view a case is served as, read as the app reads it, at the moment the page names. */
export function costCaseView(name: CostCase): HostCostView {
  const scenario = costCaseScenario(name);
  const read = readHostCost(servedHostCost(scenario.changes, scenario.endMs + 60_000));
  if (read._tag === 'Missing') throw new Error(`the ${name} case lacks ${read.field}`);
  return read.view;
}
