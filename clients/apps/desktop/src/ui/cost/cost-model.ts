/**
 * What the Cost screen draws, from the cost view the host served. This adds nothing up, orders no
 * cost and buckets nothing: every list is in the order the host sent it, every figure is a field
 * of the view said in words, and a price the host did not report stays absent (`null`) from here
 * to the screen. The only arithmetic is the layout's own: how far along its cap the spend is, and
 * which buckets get a label.
 */
import type {BarColumn, BarMark, BarRow, StackedBar, SwatchItem} from '@vibesys/ui';
import type {CostModel, CostUsage, CostView, SpendSeriesView} from '../../state/index.js';
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
  /** How many runs the figures count, for a screen that sums more than one run. */
  readonly runs?: string;
  readonly turns: string;
  readonly unpricedTurns: string;
}

export interface SpendChart {
  /** One bar for each bucket the host served, with the moment it begins and how long it lasts. */
  readonly bars: readonly StackedBar[];
  /** Labels by place under the bars, for a chart that is not on a time axis. */
  readonly marks: readonly BarMark[];
  readonly legend: readonly SwatchItem[];
  /** The span the bars cover, from the start of the first to the end of the last. */
  readonly domain: {readonly fromMs: number; readonly toMs: number};
  /** How long each bar lasts, as the host cut the series. */
  readonly bucketMs: number;
}

/** The kinds of tokens, in the order the screen lists them. */
const KINDS = [
  {key: 'input', label: 'Input', of: (usage: CostUsage) => usage.inputTokens},
  {key: 'cache-read', label: 'Cache read', of: (usage: CostUsage) => usage.cacheReadTokens},
  {key: 'cache-write', label: 'Cache write', of: (usage: CostUsage) => usage.cacheCreationTokens},
  {key: 'output', label: 'Output', of: (usage: CostUsage) => usage.outputTokens},
  {key: 'reasoning', label: 'Reasoning', of: (usage: CostUsage) => usage.reasoningTokens},
] as const;

/** What the splits and the chart are drawn from: the lists a run's cost view and a host's both carry. */
export type CostSplitSource = Pick<
  CostView,
  'total' | 'roles' | 'models' | 'series' | 'modelsTruncated'
>;

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
  /** Totals are whole, but the host left provider/model rows out to fit its bound. */
  readonly modelsTruncated: boolean;
}

/** What a row says is missing from its cost: all of it, or the turns that left it a floor. */
function noteOf(usage: CostUsage, unpricedTurns: number): string | undefined {
  if (usage.costUsd === null) return 'No price';
  return unpricedTurns > 0 ? `${formatCount(unpricedTurns)} unpriced` : undefined;
}

/**
 * The slot of the palette each model draws in. It follows the model's own key (its harness and id),
 * never the order of the data or what else is on the page, so a model keeps its color while a run
 * goes on, from one view to another and from one host to the next. A model starts at the slot its key
 * hashes to; only when another model of the same view already holds that slot does it take the next
 * free one, the keys taken in order so the same models always share the palette the same way.
 */
export function slotsOf(view: CostSplitSource): ReadonlyMap<string, number> {
  const keys = new Set<string>(view.models.map(each => modelKey(each.model)));
  for (const bucket of view.series.buckets) {
    for (const spend of bucket.byModel) keys.add(modelKey(spend.model));
  }
  if (view.modelsTruncated && view.series.buckets.some(bucket => residualCost(bucket) > 0)) {
    keys.add(OTHER_MODELS_KEY);
  }
  const slots = new Map<string, number>();
  const taken = new Set<number>();
  for (const key of [...keys].sort()) {
    let slot = slotOfKey(key);
    // More models than colors share one: the palette repeats rather than a model going without.
    for (let tried = 0; taken.has(slot) && tried < PALETTE; tried += 1) slot = (slot + 1) % PALETTE;
    taken.add(slot);
    slots.set(key, slot);
  }
  return slots;
}

/** How many colors the series palette has (`--series-0` to `--series-7`). */
const PALETTE = 8;

/**
 * Under half a cent is floating-point noise from summing the models' spend, not spend the split
 * left out: the screen writes no smaller amount than a cent, so such a residual counts as none.
 */
const RESIDUAL_FLOOR_USD = 0.005;

/** Spend included in a bucket's total but omitted from its bounded model split. */
const residualCost = (bucket: SpendSeriesView['buckets'][number]): number => {
  const residual =
    bucket.costUsd - bucket.byModel.reduce((total, spend) => total + spend.costUsd, 0);
  return residual < RESIDUAL_FLOOR_USD ? 0 : residual;
};

/** A palette key no protocol model can have: model keys always begin with a numeric harness and `/`. */
const OTHER_MODELS_KEY = 'other-models';
const OTHER_MODELS_LABEL = 'Other models';

/** The slot a key hashes to (FNV-1a), the same for the same key in every run of the program. */
function slotOfKey(key: string): number {
  let hash = 0x811c9dc5;
  for (const char of key) {
    hash = Math.imul(hash ^ (char.codePointAt(0) ?? 0), 0x01000193) >>> 0;
  }
  return hash % PALETTE;
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
  otherModelsUsd: number,
): string {
  return [
    words.moment(startMs),
    formatUsd(costUsd, 2),
    ...byModel.map(spend => `${modelName(spend.model, words)} ${formatUsd(spend.costUsd, 2)}`),
    ...(otherModelsUsd > 0 ? [`${OTHER_MODELS_LABEL} ${formatUsd(otherModelsUsd, 2)}`] : []),
  ].join('\n');
}

/** The axis is at least this many bars long, so a lone bar is one bar of a few and not the whole plot. */
const MIN_BARS = 8;

/** The labels under the bars: which bars of a series are named, and how. */
export type BarMarker = (series: SpendSeriesView) => readonly BarMark[];

/** Elapsed time from the start of the series (`0`, `6 h`, `1 d 4 h`), for one run's own chart. */
const elapsedMarks: BarMarker = ({buckets, startMs, bucketMs}) => {
  const stride = labelStride(bucketMs, buckets.length);
  return buckets.flatMap((bucket, at): BarMark[] =>
    at % stride === 0 ? [{at, text: formatElapsed(bucket.startMs - startMs)}] : [],
  );
};

/**
 * The bars of the spend over time, stacked by model, with the legend of the models and the labels
 * `marker` puts under them. `slots` is the palette slot of each model, worked out from the whole
 * view so a range of the chart never recolors a model.
 */
export function spendChart(
  view: CostSplitSource,
  slots: ReadonlyMap<string, number>,
  words: CostWords,
  marker: BarMarker,
): SpendChart | null {
  const {buckets} = view.series;
  // A bounded model split may omit every contributing model. Otherwise a priced bucket has a named
  // model. With neither anywhere there is no attributed spend to draw and an axis would be invented.
  if (
    buckets.every(
      bucket => bucket.byModel.length === 0 && !(view.modelsTruncated && residualCost(bucket) > 0),
    )
  )
    return null;
  const slot = (model: CostModel): number => slots.get(modelKey(model)) ?? 0;
  const otherSlot = slots.get(OTHER_MODELS_KEY) ?? 0;
  const bars = buckets.map((bucket): StackedBar => {
    const other = view.modelsTruncated ? residualCost(bucket) : 0;
    return {
      id: String(bucket.startMs),
      startMs: bucket.startMs,
      widthMs: view.series.bucketMs,
      total: bucket.costUsd,
      title: bucketTitle(words, bucket.startMs, bucket.costUsd, bucket.byModel, other),
      // Stacked in the palette's order, so a model keeps its place in the stack while the run goes on.
      segments: [
        ...bucket.byModel.map(spend => ({
          key: `${bucket.startMs}/${modelKey(spend.model)}`,
          series: slot(spend.model),
          value: spend.costUsd,
        })),
        ...(other > 0
          ? [
              {
                key: `${bucket.startMs}/${OTHER_MODELS_KEY}`,
                series: otherSlot,
                value: other,
              },
            ]
          : []),
      ].sort((a, b) => a.series - b.series),
    };
  });
  const legend: SwatchItem[] = [
    ...view.models.map(
      (cost): SwatchItem => ({
        // A model with no priced turn has no bar: its swatch is the flat grey of a series with none.
        label:
          cost.usage.costUsd === null
            ? `${modelName(cost.model, words)} (unpriced)`
            : modelName(cost.model, words),
        ...(cost.usage.costUsd === null ? {muted: true} : {series: slot(cost.model)}),
        mono: true,
      }),
    ),
    ...(view.modelsTruncated && buckets.some(bucket => residualCost(bucket) > 0)
      ? [{label: OTHER_MODELS_LABEL, series: otherSlot}]
      : []),
  ];
  const fromMs = buckets[0]?.startMs ?? view.series.startMs;
  const endMs = (buckets.at(-1)?.startMs ?? view.series.startMs) + view.series.bucketMs;
  return {
    bars,
    marks: marker(view.series),
    legend,
    domain: {fromMs, toMs: Math.max(endMs, fromMs + MIN_BARS * view.series.bucketMs)},
    bucketMs: view.series.bucketMs,
  };
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

const typeRows = (view: CostSplitSource, precision: Precision, words: CostWords): BarRow<4>[] =>
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
  view: CostSplitSource,
  precision: Precision,
  slots: ReadonlyMap<string, number>,
  words: CostWords,
) =>
  view.models.map(
    (cost): BarRow<3> => ({
      key: modelKey(cost.model),
      name: modelName(cost.model, words),
      mono: true,
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
    chart: spendChart(view, slots, words, elapsedMarks),
    types: typeRows(view, precision, words),
    models: modelRows(view, precision, slots, words),
    kinds: kindRows(view),
    modelsTruncated: view.modelsTruncated,
  };
}
