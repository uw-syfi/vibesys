/**
 * What the aggregate Cost screen draws, from the host cost views the hosts served. Each host works
 * out its own numbers, and this adds nothing up inside a host: a host's lists and figures are shown
 * as served. Two display choices are made over what was served (`state/orchestrator/host-cost-lists.ts`):
 * which of the host's buckets of spend the chart draws, and the order the runs are listed in. The one
 * combination is across hosts, for the facts line: the sum of the totals the hosts served, which is a
 * floor (`≥`) whenever a host's cost has unpriced turns behind it or a host has served no view yet,
 * since that host's spend is then not in it.
 *
 * A figure nobody reported is `unknown` (or `—` where there is nothing yet to report), never a zero.
 */
import type {AgentRole, RunStatus} from '@vibesys/orchestrator-proto';
import {
  bucketsInRange,
  type HostCostView,
  type RunSort,
  rangeOf,
  rangesOf,
  type SpendRange,
  sortRuns,
} from '../../state/index.js';
import {
  type AgentTypeLine,
  agentTypeLine,
  bucketWords,
  type CostCell,
  type CostWords,
  costCell,
  FLOOR,
  formatCount,
  formatUsd,
  modelKey,
  modelName,
  NOT_REPORTED,
  type Precision,
  precisionOf,
  type SpendChart,
  slotsOf,
  spendChart,
  type TokenSplit,
  tokenSplit,
  usdOrNothing,
} from '../cost/index.js';
import {plural} from '../plain-words.js';

/** The tokens of a row, split as principle 19 writes them. */
type TokenCells = TokenSplit;

/** One row of a host's runs by cost: a run, or the runs the host's list leaves out, summed. */
export interface RunLine {
  readonly key: string;
  /** The run to open; null for the row of the runs left out. */
  readonly runId: string | null;
  readonly name: string;
  readonly status: RunStatus | null;
  readonly turns: string;
  readonly tokens: TokenCells;
  readonly cost: CostCell;
  /** This run's part of the host's total cost, in whole percent. */
  readonly share: string;
  readonly budget: BudgetCell | null;
  /** When the run began, as the client holds it; null for a run it holds no card for. */
  readonly started: {readonly text: string; readonly hint: string} | null;
}

/** A run's budget, and how far along it the spend is. */
export interface BudgetCell {
  readonly text: string;
  /** The share of the budget the cost is (a floor, while unpriced turns are behind it), in whole percent. */
  readonly used: string | null;
  /** The cost is past the budget: a problem, drawn as one. */
  readonly over: boolean;
  readonly hint: string;
}

/** One row of the host's cost by model. */
export interface ModelLine {
  readonly key: string;
  readonly name: string;
  /** The palette slot of its bars; null for a model with no priced turn, which has none. */
  readonly series: number | null;
  /** The model's id and harness, whole, for the pointer. */
  readonly title: string;
  readonly tokens: TokenCells;
  readonly cost: CostCell;
}

/** The facts line over every host that served a view. */
export interface TotalFigures {
  /** The cost of the priced turns, or null when no turn is priced. */
  readonly figure: string | null;
  /** The spend leaves out turns without a price (or a host not yet read): it and the last hour are floors. */
  readonly floor: boolean;
  /** What the turns of the last hour cost, as the hosts served it; null when none served one. */
  readonly lastHour: string | null;
  /** The last hour's spend is a floor too, on the same ground as the spend. */
  readonly lastHourFloor: boolean;
  readonly runs: {readonly count: string; readonly what: string};
  readonly turns: {readonly count: string; readonly what: string};
  /** The turns with tokens and no price, or null when there are none. */
  readonly unpriced: {readonly count: string; readonly what: string} | null;
  /** Whether every host has served a view: until then the figures are partial. */
  readonly complete: boolean;
}

/** A range of the chart as a segment names it. */
interface RangeChoice {
  readonly value: SpendRange;
  readonly label: string;
}

/** Where the host's page stands: its view not read yet, read and holding no turn, or read. */
type HostCostState = 'reading' | 'empty' | 'served';

export interface HostCostScreenModel {
  readonly state: HostCostState;
  /** The view's moment, in the words' zone; null before a view is served. */
  readonly asOf: string | null;
  /** The bars of the chart for the chosen range; null while no priced turn is in it. */
  readonly chart: SpendChart | null;
  /** How long each bar lasts, for the chart's header (`per 3 hours`); null with no chart. */
  readonly bucketWords: string | null;
  /** The ranges offered, none while the spend spans a day or less. */
  readonly ranges: readonly RangeChoice[];
  /** The range the chart shows, which is the whole span whenever the one chosen is not offered. */
  readonly range: SpendRange;
  readonly runs: readonly RunLine[];
  readonly types: readonly AgentTypeLine[];
  readonly models: readonly ModelLine[];
  /** The host left out a model or shortened a model name; totals remain whole. */
  readonly modelsTruncated: boolean;
  /** What a `≥` in the lists means, said once; null while no turn is unpriced. */
  readonly unpricedNote: string | null;
}

/** The words of the run's own vocabulary this screen needs beside the cost words. */
export interface CostOverviewModelWords extends CostWords {
  /** The label of a tick of a time axis that spans `spanMs`, in ticks `stepMs` apart. */
  readonly tick: (atMs: number, spanMs: number, stepMs: number) => string;
  /** How far the display zone's clock is ahead of UTC at a moment, so the axis ticks at its midnights. */
  readonly offset: (atMs: number) => number;
  /** The day a run began, short: `Tue 3`. */
  readonly started: (atMs: number) => string;
}

const RANGE_LABELS: Readonly<Record<SpendRange, string>> = {'1d': '1d', '7d': '7d', all: 'All'};

/** A part of the whole, in whole percent; `<1%` for a part that would round to nothing. */
function shareOf(part: number | null, whole: number | null): string {
  if (part === null || whole === null || whole <= 0) return NOT_REPORTED;
  const share = part / whole;
  return share > 0 && share < 0.005 ? '<1%' : `${Math.round(share * 100)}%`;
}

/** The headline: the sum of the totals the hosts served, over `hostCount` hosts of which `views` served one. */
export function totalFigures(views: readonly HostCostView[], hostCount: number): TotalFigures {
  const costs = views.flatMap(view => (view.total.costUsd === null ? [] : [view.total.costUsd]));
  const sum = costs.reduce((all, usd) => all + usd, 0);
  const hours = views.flatMap(view => (view.lastHourUsd === null ? [] : [view.lastHourUsd]));
  const precision = precisionOf(costs.length === 0 ? null : sum);
  const unpriced = views.reduce((all, view) => all + view.unpricedTurns, 0);
  const runs = views.reduce((all, view) => all + view.runCount, 0);
  const turns = views.reduce((all, view) => all + view.turns, 0);
  return {
    figure: costs.length === 0 ? null : (usdOrNothing(sum, precision) ?? null),
    floor: costs.length > 0 && (unpriced > 0 || views.length < hostCount),
    lastHourFloor: hours.length > 0 && (unpriced > 0 || views.length < hostCount),
    lastHour:
      hours.length === 0
        ? null
        : formatUsd(
            hours.reduce((all, usd) => all + usd, 0),
            precision,
          ),
    runs: {count: formatCount(runs), what: plural(runs, 'run', 'runs')},
    turns: {count: formatCount(turns), what: plural(turns, 'turn', 'turns')},
    unpriced:
      unpriced === 0
        ? null
        : {
            count: formatCount(unpriced),
            what: plural(unpriced, 'turn unpriced', 'turns unpriced'),
          },
    complete: views.length === hostCount,
  };
}

/** What the facts line's unpriced fact says on hover. */
export const UNPRICED_FACT_HINT =
  'Turns that reported tokens and no price. Their tokens are counted and their cost is not, so ' +
  'the spend is at least what is shown.';

const runLine = (
  host: string,
  run: HostCostView['runs'][number],
  money: {readonly total: number | null; readonly precision: Precision},
  titles: Readonly<Record<string, string>>,
  startedMs: number | undefined,
  words: CostOverviewModelWords,
): RunLine => ({
  key: `${host}/${run.runId}`,
  runId: run.runId,
  name: titles[run.runId] ?? run.name,
  status: run.status,
  turns: formatCount(run.turns),
  tokens: tokenSplit(run.usage, run.turns),
  cost: costCell(run.usage, run.turns, run.unpricedTurns, money.precision),
  share: shareOf(run.usage.costUsd, money.total),
  budget: budgetCell(run.usage.costUsd, run.maxCostUsd, run.unpricedTurns > 0, money.precision),
  started:
    startedMs === undefined
      ? null
      : {text: words.started(startedMs), hint: words.moment(startedMs)},
});

/**
 * A run's budget and how far along it the spend is. Spend past the budget is a problem, whatever
 * unpriced turns are behind it (the cost is at least what is shown, so it is over by at least that).
 */
function budgetCell(
  costUsd: number | null,
  maxCostUsd: number | null,
  floor: boolean,
  precision: Precision,
): BudgetCell | null {
  if (maxCostUsd === null) return null;
  const text = formatUsd(maxCostUsd, precision);
  if (costUsd === null) return {text, used: null, over: false, hint: 'No turn reported a price.'};
  const used = `${floor ? FLOOR : ''}${Math.round((costUsd / maxCostUsd) * 100)}%`;
  const over = costUsd > maxCostUsd;
  return {
    text,
    used,
    over,
    hint: over
      ? `Over budget by ${floor ? 'at least ' : ''}${formatUsd(costUsd - maxCostUsd, precision)}`
      : `${used} of the budget spent`,
  };
}

/** What the model keeps of a view before one is served: every panel empty, in its place. */
export const NO_VIEW: HostCostScreenModel = {
  state: 'reading',
  asOf: null,
  chart: null,
  bucketWords: null,
  ranges: [],
  range: 'all',
  runs: [],
  types: [],
  models: [],
  modelsTruncated: false,
  unpricedNote: null,
};

/**
 * One host's screen: its chart (the host's buckets in the chosen range), its runs ordered as chosen
 * (the host's order until a column is pressed), and its split by type of agent and by model. `titles`
 * words the runs the client holds a card for; a run it does not hold keeps the name the host served.
 */
export function hostCostModel(
  host: string,
  view: HostCostView,
  words: CostOverviewModelWords,
  choices: {
    readonly titles?: Readonly<Record<string, string>>;
    /** When each run the client holds a card for began, by run id. */
    readonly started?: Readonly<Record<string, number>>;
    readonly range: SpendRange;
    readonly sort: RunSort;
    /** How many agents of each type are working now, from the runs' summaries the host served. */
    readonly working?: ReadonlyMap<AgentRole, number>;
  },
): HostCostScreenModel {
  const titles = choices.titles ?? {};
  const precision = precisionOf(view.total.costUsd);
  const range = rangeOf(view.series, choices.range);
  const slots = slotsOf(view);
  const total = view.total.costUsd;
  const chart = spendChart(
    {...view, series: {...view.series, buckets: bucketsInRange(view.series, view.asOfMs, range)}},
    slots,
    words,
    () => [],
  );
  const runs = sortRuns(view.runs, choices.sort).map(run =>
    runLine(host, run, {total, precision}, titles, choices.started?.[run.runId], words),
  );
  const {otherRuns} = view;
  const lines: RunLine[] =
    otherRuns === null
      ? runs
      : [
          ...runs,
          {
            key: `${host}/other-runs`,
            runId: null,
            name: `${formatCount(otherRuns.runs)} other runs`,
            status: null,
            turns: formatCount(otherRuns.turns),
            tokens: tokenSplit(otherRuns.usage, otherRuns.turns),
            cost: costCell(otherRuns.usage, otherRuns.turns, otherRuns.unpricedTurns, precision),
            share: shareOf(otherRuns.usage.costUsd, total),
            budget: null,
            started: null,
          },
        ];
  return {
    state: view.turns === 0 ? 'empty' : 'served',
    asOf: words.moment(view.asOfMs),
    chart,
    bucketWords: chart === null ? null : bucketWords(chart.bucketMs),
    ranges: rangesOf(view.series).map(value => ({value, label: RANGE_LABELS[value]})),
    range,
    unpricedNote:
      view.unpricedTurns === 0
        ? null
        : `${FLOOR.trim()} the cost leaves out ${formatCount(view.unpricedTurns)} ${plural(view.unpricedTurns, 'turn', 'turns')} with tokens and no price`,
    runs: lines,
    types: view.roles.map(role =>
      agentTypeLine({
        key: `role-${role.role}`,
        name: words.role(role.role),
        running: choices.working?.get(role.role) ?? 0,
        total: role.agents,
        spend: {turns: role.turns, unpricedTurns: role.unpricedTurns, usage: role.usage},
        precision,
      }),
    ),
    models: view.models.map(cost => ({
      key: `${cost.model.harness}/${cost.model.model}`,
      name: modelName(cost.model, words),
      series: cost.usage.costUsd === null ? null : (slots.get(modelKey(cost.model)) ?? 0),
      title:
        cost.model.model === ''
          ? `${words.harness(cost.model.harness)} default model`
          : `${cost.model.model} on ${words.harness(cost.model.harness)}`,
      tokens: tokenSplit(cost.usage, cost.turns),
      cost: costCell(cost.usage, cost.turns, cost.unpricedTurns, precision),
    })),
    modelsTruncated: view.modelsTruncated,
  };
}
