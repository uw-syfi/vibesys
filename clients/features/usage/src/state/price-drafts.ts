/**
 * The prices a person edits for one model: what they type, what it means, and the book it makes.
 *
 * A model's price is five numbers a host holds as an override, and `Map to` names another model
 * whose price this one should have. The person types text, so a draft holds text until it is
 * checked; the check says which field is wrong, in a word. A cache price left blank is the input
 * price (as a price list writes a model that has no cache price), and `0` is a price: a free model.
 *
 * A change is made against each destination's own book and the whole result is checked before
 * anything is sent: a name used twice, a model mapped to itself, or a chain of `Map to` that loops
 * (including one a change closes between entries already saved), compared the way a host compares
 * names, so what is accepted here is what the host accepts.
 *
 * Pure: no clock, no I/O.
 */
import type {
  UsageAliasEntry,
  UsagePriceBookEntry,
  UsageRateEntry,
  UsageRateOverrideEntry,
} from '@vibesys/hosts';

/** The most entries and bytes a name the host's book holds. */
export const BOOK_ENTRIES_MAX = 256;
export const BOOK_NAME_BYTES_MAX = 128;
const PRICE_MAX = 1_000_000;

/** What a person types for a price; blank cache fields default to the input price. */
export interface RateText {
  readonly input: string;
  readonly output: string;
  readonly cacheRead: string;
  readonly cacheWrite: string;
}

/** One model's edit: its price (null: automatic), where `Map to` sends it, and the destinations. */
export interface PriceDraft {
  readonly model: string;
  readonly rate: RateText;
  /** True: the model has no override and the list prices it. */
  readonly automatic: boolean;
  /** The model whose price this one takes; empty for none. */
  readonly mapTo: string;
}

export type RateField = keyof RateText;

/** The name a book compares a model by: trimmed, lowercase, without a bracketed variant. */
export function bookKey(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/\[[^\]]*\]$/, '');
}

function utf8Length(text: string): number {
  let bytes = 0;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    bytes += code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
  }
  return bytes;
}

export function textOfRate(rate: UsageRateEntry | null): RateText {
  const show = (value: number | undefined): string => (value === undefined ? '' : `${value}`);
  return {
    input: show(rate?.input),
    output: show(rate?.output),
    cacheRead: show(rate?.cacheRead),
    cacheWrite: show(rate?.cacheWrite),
  };
}

/** The draft that edits `model`, from the rate and alias it has now (`rate` null: automatic). */
export function draftOf(model: string, rate: UsageRateEntry | null, mapTo: string): PriceDraft {
  return {model, rate: textOfRate(rate), automatic: rate === null, mapTo};
}

export type RateCheck =
  | {readonly ok: true; readonly rate: UsageRateEntry}
  | {readonly ok: false; readonly field: RateField; readonly reason: string};

function number(text: string): number | null {
  if (text.trim() === '') return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : Number.NaN;
}

/** The rate a draft's text means, else the first field that is wrong and why. */
export function checkRate(text: RateText): RateCheck {
  const read = (
    field: RateField,
    required: boolean,
  ): number | null | {field: RateField; reason: string} => {
    const value = number(text[field]);
    if (value === null) return required ? {field, reason: 'required'} : null;
    if (Number.isNaN(value)) return {field, reason: 'not a number'};
    if (value < 0) return {field, reason: 'not below zero'};
    if (value > PRICE_MAX) return {field, reason: 'too large'};
    return value;
  };
  const input = read('input', true);
  const output = read('output', true);
  const cacheRead = read('cacheRead', false);
  const cacheWrite = read('cacheWrite', false);
  for (const each of [input, output, cacheRead, cacheWrite]) {
    if (typeof each === 'object' && each !== null) return {ok: false, ...each};
  }
  const base = input as number;
  return {
    ok: true,
    rate: {
      input: base,
      output: output as number,
      cacheRead: (cacheRead as number | null) ?? base,
      cacheWrite: (cacheWrite as number | null) ?? base,
      cacheWrite1h: null,
    },
  };
}

/** The first name of the book that is empty or too long, as a reason; null when all are fit. */
function badName(
  overrides: readonly UsageRateOverrideEntry[],
  aliases: readonly UsageAliasEntry[],
): string | null {
  const names = [
    ...overrides.map(each => each.model),
    ...aliases.flatMap(each => [each.from, each.to]),
  ];
  const bad = names.some(name => name.trim() === '' || utf8Length(name) > BOOK_NAME_BYTES_MAX);
  return bad ? `a model name is 1 to ${BOOK_NAME_BYTES_MAX} bytes` : null;
}

/** The aliases as the model each maps to, or why they cannot be (a name twice, or onto itself). */
function mapOf(
  aliases: readonly UsageAliasEntry[],
): {readonly map: ReadonlyMap<string, string>} | {readonly reason: string} {
  const mapped = new Map<string, string>();
  for (const each of aliases) {
    const from = bookKey(each.from);
    const to = bookKey(each.to);
    if (mapped.has(from)) return {reason: `${each.from} is mapped twice`};
    if (from === to) return {reason: `${each.from} maps to itself`};
    mapped.set(from, to);
  }
  return {map: mapped};
}

/** A model whose chain of `Map to` comes back to it, else null. */
function loopIn(mapped: ReadonlyMap<string, string>): string | null {
  for (const [start, first] of mapped) {
    let step: string | undefined = first;
    for (let taken = 0; step !== undefined; taken += 1) {
      if (step === start || taken > mapped.size) return start;
      step = mapped.get(step);
    }
  }
  return null;
}

/** Why a book cannot be saved, naming the offending entry; null when it can. */
export function checkBook(
  overrides: readonly UsageRateOverrideEntry[],
  aliases: readonly UsageAliasEntry[],
): string | null {
  if (overrides.length > BOOK_ENTRIES_MAX) return `at most ${BOOK_ENTRIES_MAX} prices`;
  if (aliases.length > BOOK_ENTRIES_MAX) return `at most ${BOOK_ENTRIES_MAX} Map to entries`;
  const name = badName(overrides, aliases);
  if (name !== null) return name;
  const seen = new Set<string>();
  for (const each of overrides) {
    const key = bookKey(each.model);
    if (seen.has(key)) return `${each.model} is priced twice`;
    seen.add(key);
  }
  const made = mapOf(aliases);
  if ('reason' in made) return made.reason;
  const loop = loopIn(made.map);
  return loop === null ? null : `${loop} maps back to itself`;
}

export interface DraftBook {
  readonly overrides: readonly UsageRateOverrideEntry[];
  readonly aliases: readonly UsageAliasEntry[];
}

/**
 * `book` after the draft's change, or why it cannot be made. An automatic draft removes the
 * model's override and its `Map to`; otherwise the override is set (`rate`) and the `Map to` set
 * or removed.
 */
export function changedBook(
  book: UsagePriceBookEntry,
  draft: PriceDraft,
  rate: UsageRateEntry | null,
): {readonly ok: true; readonly book: DraftBook} | {readonly ok: false; readonly reason: string} {
  const key = bookKey(draft.model);
  const overrides = book.overrides.filter(each => bookKey(each.model) !== key);
  const aliases = book.aliases.filter(each => bookKey(each.from) !== key);
  if (!draft.automatic && rate !== null) overrides.push({model: draft.model, rate});
  if (!draft.automatic && draft.mapTo.trim() !== '') {
    aliases.push({from: draft.model, to: draft.mapTo.trim()});
  }
  const reason = checkBook(overrides, aliases);
  return reason === null ? {ok: true, book: {overrides, aliases}} : {ok: false, reason};
}

/** The text a destination's cell shows for one price: a number, `Mixed`, `Automatic` or `Unavailable`. */
export type PriceCell =
  | {readonly kind: 'value'; readonly value: number; readonly automatic: boolean}
  | {readonly kind: 'mixed'}
  | {readonly kind: 'unavailable'};

/** Several destinations' values of one price as the one cell the dialog shows. */
export function combineCells(
  values: readonly {readonly value: number | null; readonly automatic: boolean}[],
): PriceCell {
  const known = values.filter(each => each.value !== null);
  if (known.length === 0) return {kind: 'unavailable'};
  const first = known[0];
  if (first === undefined || known.length !== values.length) return {kind: 'mixed'};
  const same = known.every(
    each => each.value === first.value && each.automatic === first.automatic,
  );
  return same && first.value !== null
    ? {kind: 'value', value: first.value, automatic: first.automatic}
    : {kind: 'mixed'};
}
