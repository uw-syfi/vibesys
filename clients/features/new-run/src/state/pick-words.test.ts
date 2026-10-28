import {describe, expect, test} from 'bun:test';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {entriesOf} from './catalogue.js';
import {
  domainFilters,
  entryName,
  fitWords,
  goalSummary,
  needsWords,
  shownEntries,
  whatItIs,
} from './pick-words.js';

const ENTRIES = entriesOf(exampleCatalogue);

describe('the examples table', () => {
  test('All counts every task and each domain its own; the counts add up', () => {
    const filters = domainFilters(ENTRIES);
    expect(filters[0]).toEqual({domain: null, label: 'All', count: ENTRIES.length});
    const domains = filters.slice(1);
    expect(domains.reduce((sum, filter) => sum + filter.count, 0)).toBe(ENTRIES.length);
    for (const filter of domains) {
      expect(filter.count).toBe(shownEntries(ENTRIES, filter.domain, '').length);
      expect(filter.count).toBeGreaterThan(0);
    }
  });

  test('a search keeps the tasks that match each of its words, in listing order', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entry = random.pick(ENTRIES);
      const word = random.pick([
        entry.task,
        entry.example.split('/').at(-1) ?? '',
        whatItIs(entry).split(' ')[0] ?? '',
      ]);
      const query = random.bool() ? word.toUpperCase() : `  ${word}  `;
      const shown = shownEntries(ENTRIES, null, query);
      expect(shown, `seed ${seed}`).toContain(entry);
      expect(shown).toEqual(ENTRIES.filter(each => shown.includes(each)));
      // A filter only ever narrows.
      const domain = random.pick(domainFilters(ENTRIES)).domain;
      expect(shownEntries(ENTRIES, domain, query).every(each => shown.includes(each))).toBe(true);
    }
    expect(shownEntries(ENTRIES, null, 'no such words anywhere')).toEqual([]);
    expect(shownEntries(ENTRIES, null, '')).toEqual(ENTRIES);
  });

  test('each task says what it is, what it needs, and its name, from the catalogue alone', () => {
    const stack = ENTRIES.find(each => each.example === 'data-structures/stack');
    const qwen = ENTRIES.find(each => each.example === 'model-serving/qwen3.5-9b-mi210');
    expect(stack === undefined || qwen === undefined).toBe(false);
    if (stack === undefined || qwen === undefined) return;
    expect(whatItIs(qwen)).toBe('Serve Qwen3.5 9B');
    expect(needsWords(qwen)).toBe('1× AMD MI210');
    expect(entryName(qwen)).toBe('qwen3.5-9b-mi210');
    expect(needsWords(stack)).toBe('CPU');
    expect(entryName(stack)).toBe(`stack · ${stack.task}`);
    // A helper without the catalogue says nothing it does not know.
    for (const entry of entriesOf(exampleCatalogueWithoutDetails)) {
      expect(whatItIs(entry)).toBe('');
      expect(needsWords(entry)).toBe('');
    }
  });

  test('a fit is claimed only when the machine fits or plainly lacks the hardware', () => {
    const hardware = {cpu_cores: 8, gpus: [], accelerator: 'cpu' as const};
    expect(fitWords({kind: 'fits', words: ''}, 'host-b', hardware)).toEqual({
      text: 'Fits host-b',
      fits: true,
    });
    expect(fitWords({kind: 'mismatch', words: ''}, 'This Mac', hardware)).toEqual({
      text: 'This Mac has no GPU',
      fits: false,
    });
    expect(fitWords({kind: 'unknown', words: ''}, 'host-b', hardware)).toBeNull();
    expect(fitWords({kind: 'mismatch', words: ''}, 'host-b', null)).toBeNull();
  });
});

describe("the goal's opening paragraph", () => {
  test('is the first prose block, with headings, lists, code and inline marks left out', () => {
    expect(
      goalSummary(
        '# Objective: serve it\n\n```\ncode\n\nmore code\n```\n\n- a list\n\nMaximize **serving throughput** for `Qwen/Qwen3.5-9B` on\n1x [MI210](https://amd.com).\n\nLater.',
      ),
    ).toBe('Maximize serving throughput for Qwen/Qwen3.5-9B on 1x MI210.');
    expect(goalSummary('Optimize a stack.\n\nHeadline metric: `ops` (maximize).\n')).toBe(
      'Optimize a stack.',
    );
    expect(goalSummary('# Only a heading\n')).toBe('');
    expect(goalSummary('')).toBe('');
  });

  test('every goal of the catalogue opens with one line of plain words', () => {
    for (const entry of ENTRIES) {
      const summary = goalSummary(entry.details?.goal ?? '');
      expect(summary).not.toMatch(/\n|\*\*|`|^#/);
    }
  });
});
