import {describe, expect, test} from 'bun:test';
import type {UsageAliasEntry, UsageRateEntry} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  BOOK_ENTRIES_MAX,
  changedBook,
  checkBook,
  checkRate,
  combineCells,
  draftOf,
} from './price-drafts.js';

/** The ways a host reads one model's name as the same name: case, padding, a bracketed variant. */
const SPELLINGS: readonly ((base: string) => string)[] = [
  base => base,
  base => base.toUpperCase(),
  base => `  ${base} `,
  base => `${base}[1m]`,
  base => `${base.toUpperCase()}[beta]`,
];
const name = (rng: Seeded, base: number): string => rng.pick(SPELLINGS)(`model-${base}`);

/** Whether a set of `from -> to` pairs over model numbers is a book a host takes, by its own rules. */
function accepted(pairs: readonly (readonly [number, number])[]): boolean {
  const next = new Map<number, number>();
  for (const [from, to] of pairs) {
    if (next.has(from) || from === to) return false;
    next.set(from, to);
  }
  for (const start of next.keys()) {
    let at = next.get(start);
    for (let steps = 0; at !== undefined; steps += 1) {
      if (at === start || steps > next.size) return false;
      at = next.get(at);
    }
  }
  return true;
}

describe('checkBook', () => {
  test('property: a book is accepted exactly when no name is mapped twice, to itself, or in a loop', () => {
    for (const seed of SEEDS.slice(0, 300)) {
      const rng = new Seeded(seed);
      const pairs = Array.from({length: rng.int(7)}, (): [number, number] => [
        rng.int(5),
        rng.int(5),
      ]);
      const aliases: UsageAliasEntry[] = pairs.map(([from, to]) => ({
        from: name(rng, from),
        to: name(rng, to),
      }));
      expect(checkBook([], aliases) === null, `seed ${seed}: ${JSON.stringify(aliases)}`).toBe(
        accepted(pairs),
      );
    }
  });

  test('a price is refused when a model is priced twice under spellings a host reads as one', () => {
    const rate: UsageRateEntry = {
      input: 1,
      output: 2,
      cacheRead: 1,
      cacheWrite: 1,
      cacheWrite1h: null,
    };
    expect(
      checkBook(
        [
          {model: 'Model-1', rate},
          {model: ' model-1[1m]', rate},
        ],
        [],
      ),
    ).toContain('priced twice');
    expect(checkBook([{model: 'model-1', rate}], [])).toBeNull();
  });

  test('a book is bounded in entries and in the bytes of a name', () => {
    const rate: UsageRateEntry = {
      input: 1,
      output: 2,
      cacheRead: 1,
      cacheWrite: 1,
      cacheWrite1h: null,
    };
    const many = Array.from({length: BOOK_ENTRIES_MAX + 1}, (_, at) => ({model: `m${at}`, rate}));
    expect(checkBook(many, [])).toContain('at most');
    expect(checkBook([{model: 'é'.repeat(65), rate}], [])).toContain('bytes');
    expect(checkBook([{model: 'é'.repeat(64), rate}], [])).toBeNull();
    expect(checkBook([{model: '  ', rate}], [])).toContain('bytes');
  });
});

describe('checkRate', () => {
  test('property: any non-negative prices are taken as typed, and blank cache prices are the input price', () => {
    for (const seed of SEEDS.slice(0, 200)) {
      const rng = new Seeded(seed);
      const input = rng.int(100_000) / 100;
      const output = rng.int(100_000) / 100;
      const cache = rng.bool() ? null : rng.int(1_000) / 100;
      const typed = cache === null ? '' : `${cache}`;
      const checked = checkRate({
        input: `${input}`,
        output: `${output}`,
        cacheRead: typed,
        cacheWrite: typed === '' ? '  ' : typed,
      });
      expect(checked, `seed ${seed}`).toEqual({
        ok: true,
        rate: {
          input,
          output,
          cacheRead: cache ?? input,
          cacheWrite: cache ?? input,
          cacheWrite1h: null,
        },
      });
    }
  });

  test('zero is a price, and an empty required price names its field', () => {
    expect(checkRate({input: '0', output: '0', cacheRead: '', cacheWrite: ''})).toMatchObject({
      ok: true,
      rate: {input: 0, cacheRead: 0},
    });
    expect(checkRate({input: '', output: '1', cacheRead: '', cacheWrite: ''})).toMatchObject({
      ok: false,
      field: 'input',
    });
  });

  test('property: a price that is not a number, below zero or beyond a million names the first field wrong', () => {
    const wrong = ['abc', '-1', '1000001', 'NaN', 'Infinity', '1e999'];
    const fields = ['input', 'output', 'cacheRead', 'cacheWrite'] as const;
    for (const seed of SEEDS.slice(0, 120)) {
      const rng = new Seeded(seed);
      const text = {input: '1', output: '1', cacheRead: '1', cacheWrite: '1'};
      const field = rng.pick(fields);
      const checked = checkRate({...text, [field]: rng.pick(wrong)});
      expect(checked, `seed ${seed}`).toMatchObject({ok: false, field});
    }
  });
});

describe('changedBook', () => {
  const rate: UsageRateEntry = {
    input: 3,
    output: 15,
    cacheRead: 0.3,
    cacheWrite: 3.75,
    cacheWrite1h: null,
  };

  test('property: an automatic draft removes the model’s price and Map to, whatever its spelling, and nothing else', () => {
    for (const seed of SEEDS.slice(0, 150)) {
      const rng = new Seeded(seed);
      const target = rng.int(4);
      const others = [0, 1, 2, 3].filter(each => each !== target);
      const book = {
        revision: 3,
        overrides: [target, ...others.slice(0, 2)].map(each => ({model: name(rng, each), rate})),
        aliases: [{from: name(rng, target), to: name(rng, 9)}],
      };
      const made = changedBook(book, draftOf(name(rng, target), null, ''), null);
      expect(made.ok, `seed ${seed}`).toBe(true);
      if (!made.ok) continue;
      expect(
        made.book.overrides.map(each =>
          each.model
            .trim()
            .toLowerCase()
            .replace(/\[.*\]$/, ''),
        ),
      ).toEqual(others.slice(0, 2).map(each => `model-${each}`));
      expect(made.book.aliases).toEqual([]);
    }
  });

  test('a change that closes a loop between entries already saved is refused, naming the entry', () => {
    const book = {revision: 1, overrides: [], aliases: [{from: 'model-a', to: 'model-b'}]};
    const made = changedBook(book, draftOf('model-b', rate, 'Model-A'), rate);
    expect(made).toMatchObject({ok: false});
    if (!made.ok) expect(made.reason).toContain('maps back to itself');
  });
});

describe('combineCells', () => {
  test('property: destinations that agree are one number, differing ones are Mixed, none is Unavailable', () => {
    for (const seed of SEEDS.slice(0, 150)) {
      const rng = new Seeded(seed);
      const values = Array.from({length: 1 + rng.int(3)}, () => ({
        value: rng.bool() ? rng.int(3) : null,
        automatic: rng.bool(),
      }));
      const cell = combineCells(values);
      const known = values.filter(each => each.value !== null);
      if (known.length === 0) expect(cell.kind).toBe('unavailable');
      else if (
        known.length === values.length &&
        known.every(
          each => each.value === known[0]?.value && each.automatic === known[0]?.automatic,
        )
      ) {
        expect(cell).toMatchObject({kind: 'value', value: known[0]?.value});
      } else expect(cell.kind).toBe('mixed');
    }
  });
});
