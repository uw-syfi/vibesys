/**
 * The two dialogs of a model: what it cost and how it is priced (`modelDetail`), and the prices a
 * person is editing for it (`pricesView`). Both are folds of what the hosts answered; the prices
 * shown are the hosts' resolved rates, so an automatic price is shown as the number the list gives
 * and was never computed here.
 *
 * A price that several destinations hold is one cell: its number when they agree, `Mixed` when
 * they differ, `Unavailable` when none has one.
 */
import type {HostKey} from '@vibesys/hosts';
import {hostFromKey, hostLabel} from '@vibesys/hosts';
import {bucketLabel, type PeriodEdges} from './edges.js';
import {dollars, percent, perMillion as perMillionText, tokens} from './format.js';
import type {SaveResult} from './machine.js';
import type {DenseSeries, HostReply, Merged} from './merge.js';
import {
  bookKey,
  checkRate,
  combineCells,
  type PriceCell,
  type PriceDraft,
  type RateField,
} from './price-drafts.js';
import {
  type ModelName,
  modelName,
  type Part,
  providerName,
  type ViewFilters,
} from './usage-view.js';

export interface PriceRowView {
  readonly field: RateField;
  readonly label: string;
  readonly cell: PriceCell;
  /** The cell in words: a number, `Mixed`, `Unavailable`; `Automatic` beside a number the list gave. */
  readonly text: string;
  readonly destinations: readonly {
    readonly host: HostKey;
    readonly label: string;
    readonly text: string;
  }[];
}

export interface ModelDetail {
  readonly key: string;
  readonly provider: string;
  readonly providerName: string;
  readonly id: string;
  readonly name: string;
  /** What it cost, or `Unpriced` when no price covers any of its tokens. */
  readonly cost: string;
  /** False when no price covers any of its tokens: there is no cost to chart or to split. */
  readonly priced: boolean;
  readonly tokens: string;
  readonly turns: number;
  readonly perMillion: string | null;
  readonly cacheHit: string | null;
  readonly trend: readonly number[];
  readonly trendLabels: readonly string[];
  readonly costParts: readonly Part[];
  readonly tokenParts: readonly Part[];
  /** `N tokens have no known price` when some tokens are unpriced; null otherwise. */
  readonly unpriced: string | null;
  readonly prices: readonly PriceRowView[];
  readonly overridden: boolean;
}

const FIELDS: readonly {readonly field: RateField; readonly label: string}[] = [
  {field: 'input', label: 'Input'},
  {field: 'output', label: 'Output'},
  {field: 'cacheRead', label: 'Cache read'},
  {field: 'cacheWrite', label: 'Cache write'},
];

function processed(each: DenseSeries): number[] {
  return each.values.cached.map(
    (cached, at) =>
      cached +
      (each.values.cacheWrite[at] ?? 0) +
      (each.values.uncached[at] ?? 0) +
      (each.values.output[at] ?? 0),
  );
}

const sum = (values: readonly number[]): number =>
  values.reduce((total, value) => total + value, 0);

function textOf(cell: PriceCell): string {
  if (cell.kind === 'mixed') return 'Mixed';
  if (cell.kind === 'unavailable') return 'Unavailable';
  return cell.automatic ? `Automatic ${perMillionText(cell.value)}` : perMillionText(cell.value);
}

/** The price rows of `provider`'s `id` over the hosts that answered. */
export function priceRows(
  replies: readonly HostReply[],
  provider: string,
  id: string,
): readonly PriceRowView[] {
  return FIELDS.map(({field, label}) => {
    const per = replies.map(reply => {
      const resolved = reply.summary.rates.find(
        each => each.provider === provider && each.model === id && each.speed === 'standard',
      );
      return {
        host: reply.host,
        value: resolved?.rate?.[field] ?? null,
        automatic: resolved?.source === 'model',
      };
    });
    const cell = combineCells(per);
    return {
      field,
      label,
      cell,
      text: textOf(cell),
      destinations: per.map(each => ({
        host: each.host,
        label: hostLabel(hostFromKey(each.host)),
        text:
          each.value === null
            ? 'Unavailable'
            : each.automatic
              ? `Automatic ${perMillionText(each.value)}`
              : perMillionText(each.value),
      })),
    };
  });
}

/** The detail of the model row `key` (a provider and a model id) under the page's filters. */
export function modelDetail(
  merged: Merged,
  replies: readonly HostReply[],
  filters: ViewFilters,
  key: string,
  names: readonly ModelName[],
  edges: PeriodEdges,
): ModelDetail | null {
  const [provider = '', id = ''] = key.split('\u0000');
  const own = merged.series.filter(
    each =>
      each.provider === provider &&
      each.model === id &&
      (filters.source === 'all' || each.origin === filters.source),
  );
  if (own.length === 0) return null;
  const n = merged.buckets;
  const column = (field: keyof DenseSeries['values']): number =>
    sum(own.flatMap(each => each.values[field]));
  const cost = column('estimateUsd');
  const volume = own.map(processed);
  const count = sum(volume.flat());
  const cached = column('cached');
  const input = cached + column('uncached') + column('cacheWrite');
  const unpriced = sum(
    own.filter(each => each.source === 'unpriced').flatMap(each => processed(each)),
  );
  const unpricedAll = count > 0 && unpriced >= count;
  const trend = new Array<number>(n).fill(0);
  for (const each of own) {
    const values = filters.tab === 'cost' ? each.values.estimateUsd : processed(each);
    values.forEach((value, at) => {
      trend[at] = (trend[at] ?? 0) + value;
    });
  }
  const part = (k: string, label: string, value: number, figure: string): Part => ({
    key: k,
    label,
    value,
    figure,
  });
  return {
    key,
    provider,
    providerName: providerName(names, provider),
    id,
    name: modelName(names, provider, id),
    cost: unpricedAll ? 'Unpriced' : dollars(cost),
    priced: !unpricedAll,
    tokens: tokens(count),
    turns: column('turns'),
    perMillion:
      count === 0 || cost === 0 || unpricedAll ? null : dollars((cost / count) * 1_000_000),
    cacheHit: input === 0 ? null : percent(cached / input),
    trend,
    trendLabels: Array.from({length: n}, (_, at) => bucketLabel(edges, at)),
    costParts: [
      part('input', 'Input', column('inputUsd'), dollars(column('inputUsd'))),
      part('cache-read', 'Cache read', column('cacheReadUsd'), dollars(column('cacheReadUsd'))),
      part('cache-write', 'Cache write', column('cacheWriteUsd'), dollars(column('cacheWriteUsd'))),
      part('output', 'Output', column('outputUsd'), dollars(column('outputUsd'))),
    ],
    tokenParts: [
      part('uncached', 'Uncached', column('uncached'), tokens(column('uncached'))),
      part('cached', 'Cached', cached, tokens(cached)),
      part('cache-write', 'Cache write', column('cacheWrite'), tokens(column('cacheWrite'))),
      part('output', 'Output', column('output'), tokens(column('output'))),
    ],
    unpriced: unpriced > 0 ? `${tokens(unpriced)} tokens have no known price` : null,
    prices: priceRows(replies, provider, id),
    overridden: own.some(each => each.source === 'override'),
  };
}

export interface PricesView {
  readonly key: string;
  readonly title: string;
  readonly draft: PriceDraft;
  readonly fields: readonly {
    readonly field: RateField;
    readonly label: string;
    readonly value: string;
    readonly error: boolean;
    /** What a blank cache price becomes, as a hint. */
    readonly hint: string | null;
  }[];
  readonly destinations: readonly {
    readonly host: HostKey;
    readonly label: string;
    readonly on: boolean;
    /** `Saved`, `Not saved`, `Updated elsewhere` or `Update required`; null before a save. */
    readonly result: string | null;
    readonly retry: boolean;
  }[];
  /** Models a `Map to` may name: those the hosts' lists price. */
  readonly targets: readonly string[];
  readonly canSave: boolean;
  /** The field or entry that makes the draft unsavable, in a word; null when it can be saved. */
  readonly problem: string | null;
}

const RESULTS: Record<SaveResult['outcome'], string> = {
  saved: 'Saved',
  moved: 'Updated elsewhere',
  failed: 'Not saved',
  unsupported: 'Update required',
};

/** What the prices dialog is made from besides the draft: the destinations, their results and the catalogue. */
interface PricesInputs {
  readonly apply: readonly HostKey[];
  readonly results: readonly SaveResult[];
  readonly replies: readonly HostReply[];
  readonly problem: string | null;
  readonly names: readonly ModelName[];
}

/** The dialog of the prices a person is setting for the model `key`. */
export function pricesView(key: string, draft: PriceDraft, inputs: PricesInputs): PricesView {
  const {apply, results, replies, problem, names} = inputs;
  const [provider = ''] = key.split('\u0000');
  const checked = checkRate(draft.rate);
  const invalid = !draft.automatic && !checked.ok ? checked.field : null;
  const targets = [
    ...new Set(
      replies.flatMap(reply =>
        reply.summary.rates.filter(each => each.rate !== null).map(each => each.model),
      ),
    ),
  ]
    .filter(each => bookKey(each) !== bookKey(draft.model))
    .sort();
  const writes = draft.automatic || checked.ok;
  return {
    key,
    title: modelName(names, provider, draft.model),
    draft,
    fields: FIELDS.map(({field, label}) => ({
      field,
      label,
      value: draft.rate[field],
      error: invalid === field,
      hint:
        (field === 'cacheRead' || field === 'cacheWrite') && draft.rate[field] === ''
          ? 'Input price'
          : null,
    })),
    destinations: replies.map(reply => {
      const result = results.find(each => each.host === reply.host);
      return {
        host: reply.host,
        label: hostLabel(hostFromKey(reply.host)),
        on: apply.includes(reply.host),
        result: result === undefined ? null : RESULTS[result.outcome],
        retry: result !== undefined && result.outcome !== 'saved',
      };
    }),
    targets,
    canSave: writes && problem === null && apply.length > 0,
    problem: invalid ?? problem,
  };
}
