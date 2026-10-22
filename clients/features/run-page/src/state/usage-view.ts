/**
 * The Usage face: what the run's records (`UsageRecord`, one per agent turn) add up to, in words
 * and shapes the face draws. Its controls (`UsageControls`) are the page's state. Every figure is
 * a fold of the same records (`@vibesys/run-state/usage`), so the tiles, the table and the chart
 * cannot disagree, and the totals are the run's own (the header's spend is the same turns' sum).
 *
 * Tokens are shown as the provider counts them, split four ways that add up to the header's
 * in plus out: the uncached `input` and the `cached` part of it, and the `output` that is not
 * `reasoning` and the `reasoning`. A count nobody reported is `unknown`, never zero; a cost is the
 * provider's, summed over the turns that reported one.
 */
import {isChatAgentKind, roleName} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {totalTokens} from '@vibesys/run-state';
import {
  compactTokens,
  dimensionValue,
  foldUsage,
  type UsageDimension,
  type UsageGroup,
  type UsageTotals,
  usageOverTime,
} from '@vibesys/run-state/usage';
import {type IdeaRow, ideaRowsById} from './ideas-view.js';
import {type ModelNames, modelLabel} from './model-names.js';
import {roleSeries, SERIES_SLOTS} from './role-series.js';
import type {RunModel} from './run-model.js';
import {type TokensReading, tokensReading, tokensTitle} from './token-words.js';

/** What the chart and the table's share count: the cost, or the chosen kinds of tokens. */
export type UsageMetric = 'cost' | 'tokens';

export const USAGE_METRICS: readonly {readonly value: UsageMetric; readonly label: string}[] = [
  {value: 'cost', label: 'Cost'},
  {value: 'tokens', label: 'Tokens'},
];

/** The four kinds of tokens a count can be, and the ones the Tokens metric adds up. */
export type TokenKind = 'input' | 'cached' | 'output' | 'reasoning';

export const TOKEN_KINDS: readonly {readonly value: TokenKind; readonly label: string}[] = [
  {value: 'input', label: 'Input'},
  {value: 'cached', label: 'Cached'},
  {value: 'output', label: 'Output'},
  {value: 'reasoning', label: 'Reasoning'},
];

/** Each slice of time on its own, or what the run had spent by the end of each. */
export type UsageChart = 'slice' | 'running';

export const USAGE_CHARTS: readonly {readonly value: UsageChart; readonly label: string}[] = [
  {value: 'slice', label: 'Per time slice'},
  {value: 'running', label: 'Running total'},
];

/** The dimensions the face groups by. */
export type UsageGroupBy = Extract<UsageDimension, 'role' | 'model' | 'harness' | 'idea'>;

export const USAGE_GROUPS: readonly {readonly value: UsageGroupBy; readonly label: string}[] = [
  {value: 'role', label: 'Role'},
  {value: 'model', label: 'Model'},
  {value: 'harness', label: 'Harness'},
  {value: 'idea', label: 'Idea'},
];

export interface UsageControls {
  readonly by: UsageGroupBy;
  /** A second dimension within the first; null: none. */
  readonly within: UsageGroupBy | null;
  readonly metric: UsageMetric;
  /** The kinds the Tokens metric adds up: never none. */
  readonly kinds: readonly TokenKind[];
  readonly chart: UsageChart;
  /** The group the reader follows (its id): its series stands out in the chart. */
  readonly highlight: string | null;
  /** The groups unfolded into the second grouping (their ids). */
  readonly open: readonly string[];
}

export const USAGE_CONTROLS_DEFAULT: UsageControls = {
  by: 'role',
  within: null,
  metric: 'cost',
  kinds: ['input', 'cached', 'output', 'reasoning'],
  chart: 'slice',
  highlight: null,
  open: [],
};

/** `controls` grouped by `by`: the second dimension is dropped if it equals it, and no group is kept. */
export function groupedBy(controls: UsageControls, by: UsageGroupBy): UsageControls {
  return {
    ...controls,
    by,
    within: controls.within === by ? null : controls.within,
    highlight: null,
    open: [],
  };
}

/** `controls` with the second grouping set; the groups fold again. */
export function groupedWithin(controls: UsageControls, within: UsageGroupBy | null): UsageControls {
  return {...controls, within, open: []};
}

/** `controls` with `kind` added to or taken from the tokens counted; the last one stays. */
export function withKind(controls: UsageControls, kind: TokenKind): UsageControls {
  const on = controls.kinds.includes(kind);
  if (on && controls.kinds.length === 1) return controls;
  const kinds = TOKEN_KINDS.map(each => each.value).filter(each =>
    each === kind ? !on : controls.kinds.includes(each),
  );
  return {...controls, kinds};
}

/** `controls` with the group `id` followed, or no group when it already is. */
export function followed(controls: UsageControls, id: string): UsageControls {
  return {...controls, highlight: controls.highlight === id ? null : id};
}

/** `controls` with the group `id` unfolded or folded. */
export function unfolded(controls: UsageControls, id: string): UsageControls {
  const open = controls.open.includes(id)
    ? controls.open.filter(each => each !== id)
    : [...controls.open, id];
  return {...controls, open};
}

/** What the face needs from the run besides its records: which idea a turn served. */
export interface UsageContext {
  /** The idea (its id) each turn span of the run served. */
  readonly ideaOfSpan: ReadonlyMap<string, string>;
  /** Each idea's row of the Ideas list, in the order the ideas started. */
  readonly ideas: ReadonlyMap<string, IdeaRow>;
  /** The names of the models the run used; a model without one reads as its id. */
  readonly models: ModelNames;
}

/** The ideas of `model` and the turns each one ran, with the names of the models `names` gives. */
export function usageContext(model: RunModel, names: ModelNames): UsageContext {
  const ideaOfSpan = new Map<string, string>();
  for (const idea of model.candidates.candidates) {
    for (const attempt of idea.attempts) {
      for (const step of attempt.steps) ideaOfSpan.set(step.span, idea.id);
    }
  }
  const rows = ideaRowsById(model);
  const ideas = new Map(
    model.candidates.candidates.flatMap(idea => {
      const row = rows.get(idea.id);
      return row === undefined ? [] : [[idea.id, row] as const];
    }),
  );
  return {ideaOfSpan, ideas, models: names};
}

const UNKNOWN = 'unknown';
const RUN_SETUP = 'Run setup';

/** The kinds of tokens a tally splits into; a kind nobody reported is `null`. */
function split(totals: UsageTotals): Readonly<Record<TokenKind, number | null>> {
  const {input, cacheRead, output, reasoning} = totals.tokens;
  return {
    input: input === null ? null : input - (cacheRead ?? 0),
    cached: cacheRead,
    output: output === null ? null : output - (reasoning ?? 0),
    reasoning,
  };
}

function metricValue(totals: UsageTotals, controls: UsageControls): number | null {
  if (controls.metric === 'cost') return totals.costUsd;
  const parts = split(totals);
  const counted = controls.kinds.map(kind => parts[kind]);
  return counted.every(count => count === null)
    ? null
    : counted.reduce<number>((sum, count) => sum + (count ?? 0), 0);
}

function dollars(amount: number | null): string {
  return amount === null ? UNKNOWN : `$${amount.toFixed(2)}`;
}

function count(amount: number | null): string {
  return amount === null ? UNKNOWN : compactTokens(amount);
}

function figure(value: number | null, controls: UsageControls): string {
  return controls.metric === 'cost' ? dollars(value) : count(value);
}

/** One part of a group's name: words, or an identifier a person may copy (a model id). */
export interface UsageLabel {
  readonly text: string;
  readonly id: boolean;
}

function labelOf(dimension: UsageDimension, value: string | null, ctx: UsageContext): UsageLabel {
  switch (dimension) {
    case 'role':
      return value === null ? {text: UNKNOWN, id: false} : {text: roleName(value), id: false};
    case 'idea':
      if (value === null) return {text: RUN_SETUP, id: false};
      return {text: ctx.ideas.get(value)?.title ?? value, id: false};
    case 'model': {
      if (value === null) return {text: UNKNOWN, id: false};
      const name = ctx.models.get(value);
      return name === undefined ? {text: value, id: true} : {text: name, id: false};
    }
    default:
      return value === null ? {text: UNKNOWN, id: false} : {text: value, id: true};
  }
}

const groupId = (value: string | null): string => JSON.stringify(value);

export interface UsageRowView {
  readonly id: string;
  readonly kind: 'group' | 'sub' | 'total';
  readonly label: readonly UsageLabel[];
  /** The group's series, drawn before its name; null for a row that is not a group. */
  readonly swatch: number | null;
  /** An idea's result, as the Ideas list words it; null for any other row. */
  readonly result: {readonly text: string; readonly tone: 'ok' | 'err' | 'muted'} | null;
  readonly turns: string;
  readonly input: string;
  readonly cached: string;
  readonly output: string;
  readonly reasoning: string;
  readonly cost: string;
  /** The row's share of the chosen metric, `42%`, and as 0 to 1 for its bar; empty when unknown. */
  readonly share: string;
  readonly shareValue: number;
  /** A group that can unfold into the second grouping: whether it is open. */
  readonly open: boolean | null;
}

export interface UsageTile {
  readonly name: string;
  readonly figure: string;
  readonly detail: string;
  /** The counts in full, for the pointer. */
  readonly title: string;
}

export interface UsageSlice {
  readonly id: string;
  readonly segments: readonly {
    readonly key: string;
    readonly series: number;
    readonly value: number;
    readonly dim: boolean;
  }[];
  readonly total: number;
  /** The slice's time and what each group spent in it, in full. */
  readonly title: string;
}

export interface UsageView {
  /** No records: the face says so instead of drawing empty figures. */
  readonly empty: boolean;
  readonly tiles: readonly UsageTile[];
  readonly slices: readonly UsageSlice[];
  /** The stack of each group over the slices, for the running total's areas. */
  readonly layers: readonly {
    readonly key: string;
    readonly series: number;
    readonly values: readonly number[];
    readonly dim: boolean;
  }[];
  readonly legend: readonly {
    readonly label: string;
    readonly series: number;
    readonly id: boolean;
  }[];
  readonly unit: string;
  /** The stretch of time the slices cover, in words; null when no record has an end time. */
  readonly span: {readonly from: string; readonly to: string} | null;
  /** Turns with no end time, which the chart leaves out; 0 when every turn is placed. */
  readonly undated: number;
  readonly rows: readonly UsageRowView[];
  /** The result column shows: the table is grouped by idea. */
  readonly results: boolean;
  /** Turns that reported no cost: they count as unknown, never zero. */
  readonly unpriced: number;
}

/** How many slices of time the chart cuts the run into. */
const SLICES = 24;

const WHEN = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

function tiles(
  total: UsageTotals,
  records: number,
  ideas: number,
  unpriced: number,
): readonly UsageTile[] {
  const parts = split(total);
  const reading: TokensReading | null = tokensReading(total.tokens);
  const input = total.tokens.input;
  const cached = total.tokens.cacheRead;
  const spent = totalTokens(total.tokens);
  const share = input === null || cached === null || input === 0 ? null : cached / input;
  return [
    {
      name: 'Cost',
      figure: dollars(total.costUsd),
      detail: unpriced === 0 ? '' : `${unpriced} unpriced`,
      title: '',
    },
    {
      name: 'Tokens',
      figure: spent === null ? UNKNOWN : compactTokens(spent),
      detail: `in ${count(input)} · out ${count(parts.output)} · reasoning ${count(parts.reasoning)}`,
      title: reading === null ? '' : tokensTitle(total.tokens),
    },
    {
      name: 'Cached input',
      figure: share === null ? UNKNOWN : `${Math.round(share * 100)}%`,
      detail: `${count(cached)} of ${count(input)} read from cache`,
      title: '',
    },
    {
      name: 'Per idea',
      figure: total.costUsd === null || ideas === 0 ? UNKNOWN : dollars(total.costUsd / ideas),
      detail: `${records} ${records === 1 ? 'turn' : 'turns'} over ${ideas} ${ideas === 1 ? 'idea' : 'ideas'}`,
      title: '',
    },
  ];
}

interface RowSpec {
  readonly group: UsageGroup;
  readonly kind: UsageRowView['kind'];
  readonly id: string;
  readonly label: readonly UsageLabel[];
  readonly swatch?: number;
  readonly result?: UsageRowView['result'];
  readonly open?: boolean;
}

function rowOf(spec: RowSpec, total: number | null, controls: UsageControls): UsageRowView {
  const {group} = spec;
  const parts = split(group.totals);
  const value = metricValue(group.totals, controls);
  const known = value !== null && total !== null && total !== 0;
  const shareValue = known ? value / total : 0;
  return {
    id: spec.id,
    kind: spec.kind,
    label: spec.label,
    swatch: spec.swatch ?? null,
    result: spec.result ?? null,
    open: spec.open ?? null,
    turns: String(group.totals.turns),
    input: count(group.totals.tokens.input),
    cached: count(group.totals.tokens.cacheRead),
    output: count(parts.output),
    reasoning: count(group.totals.tokens.reasoning),
    cost: dollars(group.totals.costUsd),
    share: known ? `${Math.round(shareValue * 100)}%` : '',
    shareValue,
  };
}

/** Groups in the order the face lists them: ideas as they started, any other by what they spent. */
function ordered(
  groups: readonly UsageGroup[],
  by: UsageGroupBy,
  controls: UsageControls,
  ctx: UsageContext,
): readonly UsageGroup[] {
  if (by === 'idea') {
    const order = [...ctx.ideas.keys()];
    const at = (group: UsageGroup): number => {
      const key = group.key[0] ?? null;
      return key === null ? -1 : order.indexOf(key);
    };
    return [...groups].sort((a, b) => at(a) - at(b));
  }
  return [...groups].sort(
    (a, b) => (metricValue(b.totals, controls) ?? -1) - (metricValue(a.totals, controls) ?? -1),
  );
}

function slicesOf(
  records: readonly UsageRecord[],
  keys: readonly (string | null)[],
  colors: ReadonlyMap<string | null, number>,
  controls: UsageControls,
  labels: ReadonlyMap<string | null, string>,
  ideaOf: (record: UsageRecord) => string | null,
): Pick<UsageView, 'slices' | 'layers' | 'span' | 'undated'> {
  const series = usageOverTime(records, controls.by, SLICES, ideaOf);
  const per = series.buckets.map(bucket =>
    keys.map(key => {
      const group = bucket.groups.find(each => (each.key[0] ?? null) === key);
      return group === undefined ? 0 : (metricValue(group.totals, controls) ?? 0);
    }),
  );
  const running = controls.chart === 'running';
  const shown = per.map((slice, at) =>
    running
      ? slice.map((_, layer) => per.slice(0, at + 1).reduce((s, p) => s + (p[layer] ?? 0), 0))
      : slice,
  );
  const dim = (key: string | null): boolean =>
    controls.highlight !== null && controls.highlight !== groupId(key);
  const unit = (value: number): string => figure(value, controls);
  const slices = series.buckets.map((bucket, at): UsageSlice => {
    const values = shown[at] ?? [];
    const segments = keys.flatMap((key, layer) => {
      const value = values[layer] ?? 0;
      return value === 0
        ? []
        : [{key: groupId(key), series: colors.get(key) ?? 0, value, dim: dim(key)}];
    });
    const lines = keys.flatMap((key, layer) =>
      (values[layer] ?? 0) === 0 ? [] : [`${labels.get(key)}: ${unit(values[layer] ?? 0)}`],
    );
    const all = segments.reduce((sum, segment) => sum + segment.value, 0);
    const head = `${WHEN.format(bucket.from)} to ${WHEN.format(bucket.to)}${running ? ' (running total)' : ''}`;
    return {
      id: `slice-${at}`,
      segments,
      total: all,
      title: [head, ...lines, `All: ${unit(all)}`].join('\n'),
    };
  });
  const first = series.buckets[0];
  const last = series.buckets.at(-1);
  return {
    slices,
    layers: keys.map((key, layer) => ({
      key: groupId(key),
      series: colors.get(key) ?? 0,
      values: shown.map(slice => slice[layer] ?? 0),
      dim: dim(key),
    })),
    span:
      first === undefined || last === undefined
        ? null
        : {from: WHEN.format(first.from), to: WHEN.format(last.to)},
    undated: series.undated.turns,
  };
}

interface RowsInput {
  readonly records: readonly UsageRecord[];
  readonly groups: readonly UsageGroup[];
  readonly colors: ReadonlyMap<string | null, number>;
  readonly total: number | null;
  readonly controls: UsageControls;
  readonly ctx: UsageContext;
  readonly ideaOf: (record: UsageRecord) => string | null;
  readonly whole: UsageTotals;
}

/** The rows of the second grouping within the group `key` (`id` is its row's). */
function subRows(input: RowsInput, key: string | null, id: string): UsageRowView[] {
  const {records, total, controls, ctx, ideaOf} = input;
  const {within} = controls;
  if (within === null) return [];
  const inside = records.filter(record => dimensionValue(record, controls.by, ideaOf) === key);
  return ordered(foldUsage(inside, [within], ideaOf).groups, within, controls, ctx).map(child => {
    const value = child.key[0] ?? null;
    return rowOf(
      {
        group: child,
        kind: 'sub',
        id: `${id}/${groupId(value)}`,
        label: [labelOf(within, value, ctx)],
      },
      total,
      controls,
    );
  });
}

/** The table's rows: each group, its second grouping when unfolded, and the run's total. */
function rowsOf(input: RowsInput): UsageRowView[] {
  const {groups, colors, total, controls, ctx} = input;
  const {within} = controls;
  const rows: UsageRowView[] = [];
  for (const group of groups) {
    const key = group.key[0] ?? null;
    const id = groupId(key);
    const idea = key === null ? undefined : ctx.ideas.get(key);
    const open = within !== null && controls.open.includes(id);
    rows.push(
      rowOf(
        {
          group,
          kind: 'group',
          id,
          label: [labelOf(controls.by, key, ctx)],
          swatch: colors.get(key) ?? 0,
          ...(controls.by === 'idea' && idea !== undefined
            ? {result: {text: idea.figure, tone: idea.figureTone}}
            : {}),
          ...(within === null ? {} : {open}),
        },
        total,
        controls,
      ),
    );
    if (open) rows.push(...subRows(input, key, id));
  }
  rows.push(
    rowOf(
      {
        group: {key: [], totals: input.whole},
        kind: 'total',
        id: 'total',
        label: [{text: 'Run total', id: false}],
      },
      total,
      controls,
    ),
  );
  return rows;
}

/** The Usage face's view of `records` under `controls`, for the run `ctx` describes. */
export function usageView(
  records: readonly UsageRecord[],
  controls: UsageControls,
  ctx: UsageContext,
): UsageView {
  const ideaOf = (record: UsageRecord): string | null =>
    record.span_id === null ? null : (ctx.ideaOfSpan.get(record.span_id) ?? null);
  const fold = foldUsage(records, [controls.by], ideaOf);
  const total = metricValue(fold.total, controls);
  const groups = ordered(fold.groups, controls.by, controls, ctx);
  const keys = groups.map(group => group.key[0] ?? null);
  const colors = new Map(
    keys.map((key, at) => [
      key,
      controls.by === 'role' && key !== null ? roleSeries(key) : at % SERIES_SLOTS,
    ]),
  );
  const labels = new Map(keys.map(key => [key, labelOf(controls.by, key, ctx).text] as const));
  const rows = rowsOf({records, groups, colors, total, controls, ctx, ideaOf, whole: fold.total});
  const chart = slicesOf(records, keys, colors, controls, labels, ideaOf);
  const unpriced = records.filter(record => record.cost_usd === null).length;
  return {
    empty: records.length === 0,
    tiles: tiles(fold.total, fold.total.turns, ctx.ideas.size, unpriced),
    ...chart,
    legend: keys.map(key => {
      const {text, id} = labelOf(controls.by, key, ctx);
      return {label: text, series: colors.get(key) ?? 0, id};
    }),
    unit: controls.metric === 'cost' ? 'US dollars' : 'tokens',
    rows,
    results: controls.by === 'idea',
    unpriced,
  };
}

/**
 * The agents of the run by role, for the header: each role with the harness and model pairs its
 * turns ran on, in the order the roles first spent. A turn whose execution start the journal
 * lacks names nothing, so it adds no pair. A chat session is no role of the run: the records keep
 * what it spent, and the header does not list it.
 */
export function roleAgents(
  records: readonly UsageRecord[],
  names: ModelNames,
): readonly {
  readonly role: string;
  readonly ran: readonly {
    harness: string | null;
    model: string | null;
    client: 'scripted' | null;
  }[];
}[] {
  const by = new Map<
    string,
    Map<string, {harness: string | null; model: string | null; client: 'scripted' | null}>
  >();
  for (const record of records) {
    const scripted = record.client === 'scripted';
    if (record.role === null || (!scripted && record.harness === null && record.model === null))
      continue;
    if (isChatAgentKind(record.role)) continue;
    const pairs = by.get(record.role) ?? new Map();
    const pair = scripted
      ? {harness: null, model: null, client: 'scripted' as const}
      : {harness: record.harness, model: record.model, client: null};
    pairs.set(`${pair.client}\n${pair.harness}\n${pair.model}`, pair);
    by.set(record.role, pairs);
  }
  return [...by.entries()].map(([role, pairs]) => ({
    role: roleName(role),
    ran: [...pairs.values()].map(({harness, model, client}) => ({
      harness,
      model: model === null ? null : modelLabel(names, model),
      client,
    })),
  }));
}
