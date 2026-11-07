/**
 * What the Cost screen draws, from the cost view the host served. This adds nothing up, orders no
 * cost and buckets nothing: every list is in the order the host sent it, every figure is a field
 * of the view said in words, and a price the host did not report stays absent (`null`) from here
 * to the screen. The only arithmetic is the layout's own: how far along its cap the spend is, and
 * which buckets get a label.
 */
import type {BarColumn, BarMark, BarRow, StackedBar, SwatchItem} from '@vibesys/ui';
import type {CostModel, CostUsage, CostView} from '../../state/index.js';
import {
  type CostWords,
  formatCount,
  formatElapsed,
  formatRate,
  formatTokens,
  formatUsd,
  labelStride,
  modelKey,
  modelName,
  type Precision,
  precisionOf,
  tokensOrNothing,
  usdOrNothing,
} from './words.js';

/** The headline: what the run has cost against its cap, and how fast it is spending. */
export interface SpendFigures {
  /** The cost of the priced turns, or null when no turn is priced. */
  readonly figure: string | null;
  /** Turns without a price are behind the figure, so it is a floor. */
  readonly floor: boolean;
  /** The run's cost limit, or null when it has none. */
  readonly cap: string | null;
  /** How far along the cap the spend is, for the meter's width; null where there is none to draw. */
  readonly capShare: number | null;
  readonly lastHour: string | null;
  readonly turns: string;
  readonly unpricedTurns: string;
}

interface SpendChart {
  readonly bars: readonly StackedBar[];
  readonly marks: readonly BarMark[];
  readonly legend: readonly SwatchItem[];
}

/** The kinds of tokens, in the order the screen lists them. */
const KINDS = [
  {key: 'input', label: 'Input', of: (usage: CostUsage) => usage.inputTokens},
  {key: 'cache-read', label: 'Cache read', of: (usage: CostUsage) => usage.cacheReadTokens},
  {key: 'cache-write', label: 'Cache write', of: (usage: CostUsage) => usage.cacheCreationTokens},
  {key: 'output', label: 'Output', of: (usage: CostUsage) => usage.outputTokens},
  {key: 'reasoning', label: 'Reasoning', of: (usage: CostUsage) => usage.reasoningTokens},
] as const;

export interface CostScreenModel {
  /** No turn has finished: there is nothing to split yet. */
  readonly empty: boolean;
  readonly spend: SpendFigures;
  readonly chart: SpendChart | null;
  /** By agent type: agents, turns, tokens, cost. */
  readonly types: readonly BarRow<4>[];
  /** By model: turns, tokens, cost. */
  readonly models: readonly BarRow<3>[];
  /** By kind of token: tokens. */
  readonly kinds: readonly BarRow<1>[];
}

/** What a row says is missing from its cost: all of it, or the turns that left it a floor. */
function noteOf(usage: CostUsage, unpricedTurns: number): string | undefined {
  if (usage.costUsd === null) return 'No price';
  return unpricedTurns > 0 ? `${formatCount(unpricedTurns)} unpriced` : undefined;
}

/**
 * The slot of the palette each model draws in. It follows the models' names, not their cost, so a
 * model keeps its color while the run goes on and another overtakes it.
 */
function slotsOf(view: CostView): ReadonlyMap<string, number> {
  const keys = new Set<string>(view.models.map(each => modelKey(each.model)));
  for (const bucket of view.series.buckets) {
    for (const spend of bucket.byModel) keys.add(modelKey(spend.model));
  }
  return new Map([...keys].sort().map((key, at) => [key, at]));
}

function spendOf(view: CostView, precision: Precision): SpendFigures {
  const {costUsd} = view.total;
  const {maxCostUsd} = view;
  return {
    figure: usdOrNothing(costUsd, precision),
    floor: costUsd !== null && view.unpricedTurns > 0,
    cap: maxCostUsd === null ? null : formatUsd(maxCostUsd, precision),
    capShare: costUsd === null || maxCostUsd === null ? null : costUsd / maxCostUsd,
    lastHour: view.lastHourUsd === null ? null : formatRate(view.lastHourUsd, precision),
    turns: formatCount(view.turns),
    unpricedTurns: formatCount(view.unpricedTurns),
  };
}

/** What hovering a bucket says: when it starts, and what each model spent in it. */
function bucketTitle(
  words: CostWords,
  startMs: number,
  costUsd: number,
  byModel: readonly {readonly model: CostModel; readonly costUsd: number}[],
): string {
  return [
    words.moment(startMs),
    formatUsd(costUsd, 2),
    ...byModel.map(spend => `${modelName(spend.model, words)} ${formatUsd(spend.costUsd, 2)}`),
  ].join('\n');
}

function chartOf(
  view: CostView,
  slots: ReadonlyMap<string, number>,
  words: CostWords,
): SpendChart | null {
  const {buckets, startMs, bucketMs} = view.series;
  // A bucket has a model's spend only where a turn was priced: with none anywhere there is no
  // spend to draw, and an axis would make up a scale for it.
  if (buckets.every(bucket => bucket.byModel.length === 0)) return null;
  const slot = (model: CostModel): number => slots.get(modelKey(model)) ?? 0;
  const bars = buckets.map(
    (bucket): StackedBar => ({
      id: String(bucket.startMs),
      total: bucket.costUsd,
      title: bucketTitle(words, bucket.startMs, bucket.costUsd, bucket.byModel),
      // Stacked in the palette's order, so a model keeps its place in the stack while the run goes on.
      segments: bucket.byModel
        .map(spend => ({
          key: `${bucket.startMs}/${modelKey(spend.model)}`,
          series: slot(spend.model),
          value: spend.costUsd,
        }))
        .sort((a, b) => a.series - b.series),
    }),
  );
  const stride = labelStride(bucketMs, buckets.length);
  const marks = buckets.flatMap((bucket, at): BarMark[] =>
    at % stride === 0 ? [{at, text: formatElapsed(bucket.startMs - startMs)}] : [],
  );
  const legend = view.models.map(
    (cost): SwatchItem => ({
      label: modelName(cost.model, words),
      series: slot(cost.model),
      ...(cost.model.model === '' ? {} : {mono: true}),
    }),
  );
  return {bars, marks, legend};
}

/**
 * The headers of each table, built here beside its rows so that a header is always over the figure
 * the row puts under it, wherever the table is drawn (the Cost screen, the report's card).
 */
export const TYPE_COLUMNS = [
  {header: 'Agents'},
  {header: 'Turns'},
  {header: 'Tokens'},
  {header: 'Cost', strong: true},
] as const satisfies readonly BarColumn[];
export const MODEL_COLUMNS = [
  {header: 'Turns'},
  {header: 'Tokens'},
  {header: 'Cost', strong: true},
] as const satisfies readonly BarColumn[];
export const KIND_COLUMNS = [
  {header: 'Tokens', strong: true},
] as const satisfies readonly BarColumn[];

const typeRows = (view: CostView, precision: Precision, words: CostWords): BarRow<4>[] =>
  view.roles.map(role => ({
    key: `role-${role.role}`,
    name: words.role(role.role),
    value: role.usage.costUsd,
    ...optionalNote(noteOf(role.usage, role.unpricedTurns)),
    figures: [
      `×${formatCount(role.agents)}`,
      formatCount(role.turns),
      formatTokens(role.totalTokens),
      usdOrNothing(role.usage.costUsd, precision),
    ],
  }));

const modelRows = (
  view: CostView,
  precision: Precision,
  slots: ReadonlyMap<string, number>,
  words: CostWords,
) =>
  view.models.map(
    (cost): BarRow<3> => ({
      key: modelKey(cost.model),
      name: modelName(cost.model, words),
      ...(cost.model.model === '' ? {} : {mono: true}),
      series: slots.get(modelKey(cost.model)) ?? 0,
      value: cost.usage.costUsd,
      ...optionalNote(noteOf(cost.usage, cost.unpricedTurns)),
      figures: [
        formatCount(cost.turns),
        formatTokens(cost.totalTokens),
        usdOrNothing(cost.usage.costUsd, precision),
      ],
    }),
  );

const kindRows = (view: CostView): BarRow<1>[] =>
  KINDS.map(kind => ({
    key: kind.key,
    name: kind.label,
    value: kind.of(view.total),
    figures: [tokensOrNothing(kind.of(view.total))],
  }));

/** `{note}` when there is one, so a row without a note has no `note` key at all. */
const optionalNote = (note: string | undefined): {readonly note?: string} =>
  note === undefined ? {} : {note};

/** The screen's model of one run's cost view. */
export function costScreenModel(view: CostView, words: CostWords): CostScreenModel {
  const precision = precisionOf(view.total.costUsd);
  const slots = slotsOf(view);
  return {
    empty: view.turns === 0,
    spend: spendOf(view, precision),
    chart: chartOf(view, slots, words),
    types: typeRows(view, precision, words),
    models: modelRows(view, precision, slots, words),
    kinds: kindRows(view),
  };
}
