import {describe, expect, it} from '@effect/vitest';
import {niceScale} from '@vibesys/ui';
import fc from 'fast-check';
import {
  formatAxisUsd,
  formatCount,
  formatElapsed,
  formatRate,
  formatTokens,
  formatUsd,
  labelStride,
  modelDisplayName,
  type Precision,
  precisionOf,
} from './words.js';

const precision = fc.constantFrom<Precision>(0, 2);
const UNIT = {k: 1e3, M: 1e6, B: 1e9, T: 1e12} as const;

describe('writing tokens', () => {
  it('is exact under a thousand and otherwise one decimal of a unit that reads back to within a tenth of it', () => {
    fc.assert(
      fc.property(fc.integer({min: 0, max: 10 ** 14}), tokens => {
        const text = formatTokens(tokens);
        if (tokens < 1000) {
          expect(text).toBe(String(tokens));
          return;
        }
        const match = /^(\d{1,3}\.\d)([kMBT])$/.exec(text);
        expect(match, text).not.toBeNull();
        const [, figure = '', unit = 'k'] = match ?? [];
        const size = UNIT[unit as keyof typeof UNIT];
        expect(Math.abs(Number(figure) * size - tokens)).toBeLessThanOrEqual(size / 20 + 1e-6);
      }),
      {numRuns: 300},
    );
  });

  it('never reads 1000 of a unit: 999,950 is 1.0M', () => {
    expect(formatTokens(999_949)).toBe('999.9k');
    expect(formatTokens(999_950)).toBe('1.0M');
    expect(formatTokens(173_200_000)).toBe('173.2M');
  });
});

describe('writing dollars', () => {
  it('writes a cost above zero as something above zero, at either precision', () => {
    fc.assert(
      fc.property(fc.double({min: 1e-9, max: 1e7, noNaN: true}), precision, (usd, digits) => {
        const text = formatUsd(usd, digits);
        expect(text).not.toMatch(/^\$0(\.0+)?$/);
        expect(text.startsWith('$') || text.startsWith('<$')).toBe(true);
      }),
    );
  });

  it('reads back to within half a unit of its last digit', () => {
    fc.assert(
      fc.property(fc.double({min: 0.5, max: 1e7, noNaN: true}), precision, (usd, digits) => {
        const text = formatUsd(usd, digits);
        const back = Number(text.replace(/[$,]/g, ''));
        expect(Math.abs(back - usd)).toBeLessThanOrEqual(digits === 0 ? 0.5 : 0.005 + 1e-9);
      }),
    );
  });

  it('writes cents while the run is cheap and whole dollars once it is not', () => {
    expect(precisionOf(null)).toBe(2);
    expect(precisionOf(99.99)).toBe(2);
    expect(precisionOf(100)).toBe(0);
    expect(formatUsd(318.4, precisionOf(318.4))).toBe('$318');
    expect(formatUsd(12.5, precisionOf(12.5))).toBe('$12.50');
    expect(formatUsd(1234.5, 0)).toBe('$1,235');
    expect(formatUsd(0.001, 2)).toBe('<$0.01');
    expect(formatRate(14.2, 2)).toBe('$14.20/h');
  });

  it('writes every axis step of a tidy scale exactly', () => {
    fc.assert(
      fc.property(fc.double({min: 1e-4, max: 1e6, noNaN: true}), top => {
        const {ticks} = niceScale(top, 3);
        const step = ticks[1] ?? 1;
        const written = ticks.map(tick => formatAxisUsd(tick, step));
        ticks.forEach((tick, at) => {
          expect(Math.abs(Number((written[at] ?? '').slice(1)) - tick)).toBeLessThan(1e-9);
        });
        // One axis, one number of decimals.
        expect(new Set(written.map(text => text.split('.')[1]?.length ?? 0)).size).toBe(1);
      }),
    );
    expect([0.05, 0.1, 0.15].map(v => formatAxisUsd(v, 0.05))).toEqual(['$0.05', '$0.10', '$0.15']);
    expect([20, 40].map(v => formatAxisUsd(v, 20))).toEqual(['$20', '$40']);
    expect([2.5, 5, 7.5].map(v => formatAxisUsd(v, 2.5))).toEqual(['$2.5', '$5.0', '$7.5']);
  });
});

describe('writing counts and elapsed time', () => {
  it('separates thousands', () => {
    expect(formatCount(1433)).toBe('1,433');
    expect(formatCount(12)).toBe('12');
  });

  it('says how far into the run a bucket starts, and more time never reads as less', () => {
    expect(formatElapsed(0)).toBe('0');
    expect(formatElapsed(20 * 60_000)).toBe('20 min');
    expect(formatElapsed(80 * 60_000)).toBe('1 h 20 min');
    expect(formatElapsed(6 * 3_600_000)).toBe('6 h');
    expect(formatElapsed(28 * 3_600_000)).toBe('1 d 4 h');
    expect(formatElapsed(24 * 3_600_000)).toBe('1 d');
  });
});

describe('which buckets are labelled', () => {
  const widths = [
    60_000, 300_000, 900_000, 1_800_000, 3_600_000, 7_200_000, 21_600_000, 86_400_000,
  ];

  it('labels about six round times into the run, however long it is, starting at the first bucket', () => {
    fc.assert(
      fc.property(fc.constantFrom(...widths), fc.integer({min: 1, max: 64}), (width, buckets) => {
        const stride = labelStride(width, buckets);
        expect(stride).toBeGreaterThanOrEqual(1);
        const labelled = Math.ceil(buckets / stride);
        expect(labelled).toBeLessThanOrEqual(buckets === 1 ? 1 : 7);
        // The labels are round: the time of each is a whole number of minutes, hours or days.
        const step = stride * width;
        expect(step % 60_000).toBe(0);
      }),
    );
  });

  it('has no stride for a bucket of no length', () => {
    expect(labelStride(0, 10)).toBe(1);
  });
});

describe('naming a model', () => {
  it('words the ids of the common shapes as a person says them', () => {
    expect(modelDisplayName('claude-opus-5-5')).toBe('Claude Opus 5.5');
    expect(modelDisplayName('claude-sonnet-5-5-20260115')).toBe('Claude Sonnet 5.5');
    expect(modelDisplayName('gpt-5-codex')).toBe('GPT 5 Codex');
    expect(modelDisplayName('gemini-2.5-pro')).toBe('Gemini 2.5 Pro');
    expect(modelDisplayName('model-a')).toBe('Model A');
    expect(modelDisplayName('vendor/some-model-7b')).toBe('Some Model 7b');
  });

  it('never leaves a kebab-case id, a lowercase first letter or a stray separator in the name', () => {
    const word = fc.stringMatching(/^[a-z][a-z0-9]{0,8}$/);
    const id = fc
      .array(fc.oneof(word, fc.stringMatching(/^[0-9]{1,2}$/)), {minLength: 1, maxLength: 6})
      .map(parts => parts.join('-'));
    fc.assert(
      fc.property(id, raw => {
        const name = modelDisplayName(raw);
        expect(name).not.toMatch(/[-_]/);
        expect(name).not.toMatch(/\s{2}|^\s|\s$/);
        expect(name.charAt(0)).toBe(name.charAt(0).toUpperCase());
        // Said twice, it is the same words: naming is a function of the id alone.
        expect(modelDisplayName(raw)).toBe(name);
      }),
    );
  });
});
