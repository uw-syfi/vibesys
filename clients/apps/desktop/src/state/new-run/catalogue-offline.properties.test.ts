import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {CatalogueEntrySchema, TaskCardSchema, type TaskDomain} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  availableEntry,
  catalogueItemExample,
  catalogueItemKey,
  catalogueItems,
  groupCatalogueItems,
  REGISTERED_EXAMPLES,
  visibleCatalogueItems,
} from './index.js';

const entry = (example: string, task: string) =>
  create(CatalogueEntrySchema, {example, card: create(TaskCardSchema, {task})});

describe('the offline example catalogue', () => {
  it('lists every registered VibeSys example with no machine connected', () => {
    const items = catalogueItems([]);
    expect(items).toHaveLength(REGISTERED_EXAMPLES.length);
    expect(items.map(catalogueItemExample)).toEqual(REGISTERED_EXAMPLES.map(example => example.id));
    expect(items.every(item => item._tag === 'BrowseOnly')).toBe(true);
    expect(items.every(item => availableEntry(item) === null)).toBe(true);
  });

  it('replaces only a machine-provided example with its tasks and keeps newer host entries', () => {
    const registered = REGISTERED_EXAMPLES[0];
    if (registered === undefined) throw new Error('the release registry has examples');
    const hostOnly = entry('future-domain/future-example', 'default');
    const supplied = [entry(registered.id, 'a'), entry(registered.id, 'b'), hostOnly];
    const items = catalogueItems(supplied);
    expect(
      items
        .filter(item => catalogueItemExample(item) === registered.id)
        .map(item => availableEntry(item)?.card?.task),
    ).toEqual(['a', 'b']);
    expect(items.map(catalogueItemKey)).toContain('future-domain/future-example:default');
    expect(items.filter(item => item._tag === 'BrowseOnly')).toHaveLength(
      REGISTERED_EXAMPLES.length - 1,
    );
  });

  it('groups every offline item exactly once under its registered category', () => {
    const items = catalogueItems([]);
    const groups = groupCatalogueItems(items);
    expect(groups.flatMap(group => group.items)).toHaveLength(items.length);
    expect(new Set(groups.flatMap(group => group.items))).toEqual(new Set(items));
    expect(new Set<TaskDomain>(groups.map(group => group.domain)).size).toBe(groups.length);
  });

  it('search only narrows and finds every registered example by its path or category', () => {
    const items = catalogueItems([]);
    fc.assert(
      fc.property(
        fc.constantFrom(...REGISTERED_EXAMPLES),
        fc.boolean(),
        (registered, byCategory) => {
          const query = byCategory ? registered.category : registered.id;
          const shown = visibleCatalogueItems(items, {query});
          expect(shown.every(item => items.includes(item))).toBe(true);
          expect(shown.map(catalogueItemExample)).toContain(registered.id);
        },
      ),
    );
  });
});
