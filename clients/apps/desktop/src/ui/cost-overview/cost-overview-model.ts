/**
 * What the aggregate Cost screen draws, from the host cost views the hosts served. Each host works
 * out its own numbers, and this adds nothing up inside a host: a host's lists, order and figures are
 * shown as served. The one combination is across hosts, for the headline: the sum of the totals the
 * hosts served, which is a floor (`≥`) whenever a host's cost has unpriced turns behind it or a host
 * has served no view yet, since that host's spend is then not in it.
 */
import type {RunStatus} from '@vibesys/orchestrator-proto';
import type {HostCostView} from '../../state/index.js';
import {
  type CostSplits,
  type CostWords,
  costSplits,
  FLOOR,
  formatCount,
  formatRate,
  NOT_REPORTED,
  precisionOf,
  usdOrNothing,
} from '../cost/index.js';

/** One row of a host's runs by cost: a run, or the runs the host's list leaves out, summed. */
export interface RunLine {
  readonly key: string;
  /** The run to open; null for the row of the runs left out. */
  readonly runId: string | null;
  readonly name: string;
  readonly status: RunStatus | null;
  readonly turns: string;
  readonly cost: string;
  readonly budget: string | null;
}

/** The headline over every host that served a view. */
export interface TotalFigures {
  readonly figure: string | null;
  readonly floor: boolean;
  readonly lastHour: string | null;
  readonly runs: string;
  readonly turns: string;
  readonly unpricedTurns: string;
}

export interface HostCostScreenModel extends CostSplits {
  readonly runs: readonly RunLine[];
  /** The view's moment, in the words' zone. */
  readonly asOf: string;
  readonly empty: boolean;
}

const costWords = (usd: number | null, floor: boolean, precision: 0 | 2): string =>
  usd === null ? NOT_REPORTED : `${floor ? FLOOR : ''}${usdOrNothing(usd, precision)}`;

/** The headline: the sum of the totals the hosts served, over `hostCount` hosts of which `views` served one. */
export function totalFigures(views: readonly HostCostView[], hostCount: number): TotalFigures {
  const costs = views.flatMap(view => (view.total.costUsd === null ? [] : [view.total.costUsd]));
  const sum = costs.reduce((all, usd) => all + usd, 0);
  const hours = views.flatMap(view => (view.lastHourUsd === null ? [] : [view.lastHourUsd]));
  const precision = precisionOf(costs.length === 0 ? null : sum);
  const unpriced = views.reduce((all, view) => all + view.unpricedTurns, 0);
  return {
    figure: costs.length === 0 ? null : (usdOrNothing(sum, precision) ?? null),
    floor: costs.length > 0 && (unpriced > 0 || views.length < hostCount),
    lastHour:
      hours.length === 0
        ? null
        : formatRate(
            hours.reduce((all, usd) => all + usd, 0),
            precision,
          ),
    runs: formatCount(views.reduce((all, view) => all + view.runCount, 0)),
    turns: formatCount(views.reduce((all, view) => all + view.turns, 0)),
    unpricedTurns: formatCount(unpriced),
  };
}

/**
 * One host's screen: its runs as the host ranked them, then its chart and splits. `titles` words
 * the runs the client holds a card for; a run it does not hold keeps the name the host served.
 */
export function hostCostModel(
  host: string,
  view: HostCostView,
  words: CostWords,
  titles: Readonly<Record<string, string>> = {},
): HostCostScreenModel {
  const precision = precisionOf(view.total.costUsd);
  const runs: RunLine[] = view.runs.map(run => ({
    key: `${host}/${run.runId}`,
    runId: run.runId,
    name: titles[run.runId] ?? run.name,
    status: run.status,
    turns: formatCount(run.turns),
    cost: costWords(run.usage.costUsd, run.unpricedTurns > 0, precision),
    budget: run.maxCostUsd === null ? null : usdOrNothing(run.maxCostUsd, precision),
  }));
  const {otherRuns} = view;
  if (otherRuns !== null) {
    runs.push({
      key: `${host}/other-runs`,
      runId: null,
      name: `${formatCount(otherRuns.runs)} other runs`,
      status: null,
      turns: formatCount(otherRuns.turns),
      cost: costWords(otherRuns.usage.costUsd, otherRuns.unpricedTurns > 0, precision),
      budget: null,
    });
  }
  return {
    ...costSplits(view, words),
    runs,
    asOf: words.moment(view.asOfMs),
    empty: view.turns === 0,
  };
}
