import {describe, expect, test} from 'bun:test';
import {NO_TOKENS, type TokenTally} from '@vibesys/run-state';
import {tokensCells, tokensColumn, tokensLine, tokensReading, tokensTitle} from './token-words.js';

const TALLY: TokenTally = {
  input: 1_234_567,
  cacheRead: 1_000_000,
  cacheWrite: null,
  output: 12_345,
  reasoning: 2_000,
};

describe('a tally in words', () => {
  test('the line splits input, with its cached share, from output', () => {
    expect(tokensLine(TALLY)).toBe('in 1.2M (1M cached) · out 12.3k');
    expect(tokensLine({...TALLY, cacheRead: 6_200_000, input: 7_100_000, output: 500_000})).toBe(
      'in 7.1M (6.2M cached) · out 500k',
    );
  });

  test('a part nobody reported reads unknown, never zero', () => {
    expect(tokensLine({...TALLY, cacheRead: null})).toBe('in 1.2M (cached unknown) · out 12.3k');
    expect(tokensLine({...NO_TOKENS, output: 40})).toBe('in unknown · out 40');
    expect(tokensLine({...NO_TOKENS, input: 40})).toBe('in 40 (cached unknown) · out unknown');
    expect(tokensLine(NO_TOKENS)).toBeNull();
  });

  test('a real zero reads as zero, not as missing', () => {
    expect(tokensLine({...NO_TOKENS, input: 0, cacheRead: 0, output: 0})).toBe(
      'in 0 (0 cached) · out 0',
    );
  });

  test('the title gives the input split into cached and uncached, and the output with reasoning', () => {
    expect(tokensTitle(TALLY)).toBe(
      'Input 1,234,567 (1,000,000 cached, 234,567 uncached)\nOutput 12,345 (2,000 reasoning)\nTotal 1,246,912',
    );
  });

  test('the title adds cache writes and reasoning only when there were some', () => {
    expect(tokensTitle({...TALLY, cacheWrite: 500})).toContain('500 written to cache');
    expect(tokensTitle({...TALLY, cacheWrite: 0})).not.toContain('written');
    expect(tokensTitle({...TALLY, reasoning: 0})).not.toContain('reasoning');
    expect(tokensTitle({...TALLY, reasoning: null})).not.toContain('reasoning');
  });

  test('the title writes a missing part as unknown and gives no total without both', () => {
    expect(tokensTitle({...NO_TOKENS, output: 40})).toBe('Input unknown\nOutput 40');
    expect(tokensTitle({...TALLY, cacheRead: null})).toContain('(cached unknown)');
  });

  test('the reading pairs the line with the full counts', () => {
    expect(tokensReading(TALLY)).toEqual({
      label: 'in 1.2M (1M cached) · out 12.3k',
      title: tokensTitle(TALLY),
    });
    expect(tokensReading(NO_TOKENS)).toBeNull();
    expect(tokensReading(null)).toBeNull();
    expect(tokensReading(undefined)).toBeNull();
  });
});

describe('a tokens column', () => {
  test('says running for a running turn, unknown for one that reported nothing, and blank for a non-turn', () => {
    const cells = tokensColumn([TALLY, 'running', null, NO_TOKENS, undefined]);
    expect(cells.map(cell => cell?.label)).toEqual([
      'in 1.2M (1M cached) · out 12.3k',
      'running',
      'unknown',
      'unknown',
      '',
    ]);
  });

  test('has no cells at all when no row has a figure', () => {
    expect(tokensColumn([NO_TOKENS, null, undefined])).toEqual([null, null, null]);
    expect(tokensColumn([])).toEqual([]);
  });

  test('a run whose turns are all still running has no column yet', () => {
    expect(tokensColumn(['running', undefined])).toEqual([null, null]);
    expect(tokensCells(['running', null])).toEqual([null, null]);
  });
});

describe('the in and out columns', () => {
  test('give the input with its cached share beside it, and the output apart', () => {
    const [cells] = tokensCells([TALLY]);
    expect(cells?.input).toMatchObject({label: '1.2M', note: '1M cached'});
    expect(cells?.output).toMatchObject({label: '12.3k', note: ''});
    expect(cells?.input.title).toBe(tokensTitle(TALLY));
  });

  test('write a missing part as unknown, never zero, and a running turn as running', () => {
    const [noCache, running, gate, unreported] = tokensCells([
      {...TALLY, cacheRead: null},
      'running',
      undefined,
      null,
    ]);
    expect(noCache?.input).toMatchObject({label: '1.2M', note: 'cached unknown'});
    expect(running?.input.label).toBe('running');
    expect(gate?.input.label).toBe('');
    expect(unreported?.input.label).toBe('unknown');
    expect(unreported?.output.label).toBe('unknown');
  });

  test('have no cells when no row has a figure', () => {
    expect(tokensCells([null, undefined])).toEqual([null, null]);
  });
});
