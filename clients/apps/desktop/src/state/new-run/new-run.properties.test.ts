import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  type CatalogueEntry,
  CatalogueEntrySchema,
  type EvalPlace,
  EvalPlaceKind,
  EvalPlaceSchema,
  type Fit,
  FitSchema,
  FitState,
  OffersSource,
  OffersUnreadableReason,
  TaskCardSchema,
  TaskDomain,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  autoPick,
  defaultName,
  domainOf,
  entryKey,
  fitWord,
  folderName,
  groupByDomain,
  placeChoice,
  placeFits,
  placeWord,
  readable,
  visibleEntries,
} from './index.js';

const NAMES = ['a', 'b', 'c', 'd', 'e'] as const;

const domainArb = fc.constantFrom(
  TaskDomain.UNSPECIFIED,
  TaskDomain.LLM_SERVING,
  TaskDomain.DATA_STRUCTURES,
  TaskDomain.DATABASES,
  TaskDomain.MICROSERVICES,
  99 as TaskDomain,
);

const fitArb: fc.Arbitrary<Fit> = fc
  .record({
    state: fc.constantFrom(
      FitState.FITS,
      FitState.NO_ACCELERATOR,
      FitState.UNKNOWN_ACCELERATOR,
      FitState.PLACES_UNREADABLE,
    ),
    places: fc.uniqueArray(fc.constantFrom(...NAMES)),
  })
  .map(fields => create(FitSchema, fields));

const entryArb: fc.Arbitrary<CatalogueEntry> = fc
  .record({
    folder: fc.stringMatching(/^[a-z][a-z0-9-]{0,8}$/),
    group: fc.constantFrom('model-serving', 'data-structures', 'databases'),
    domain: domainArb,
    heading: fc.constantFrom('', 'Serve faster', 'A faster map'),
    fit: fitArb,
  })
  .map(({folder, group, domain, heading, fit}) =>
    create(CatalogueEntrySchema, {
      example: `${group}/${folder}`,
      card: create(TaskCardSchema, {task: folder, domain, heading}),
      fit,
    }),
  );

const entriesArb = fc.array(entryArb, {maxLength: 25});

const placeArb = (name: (typeof NAMES)[number]): fc.Arbitrary<EvalPlace> =>
  fc.boolean().map(known =>
    create(EvalPlaceSchema, {
      name,
      kind: EvalPlaceKind.SSH,
      state: known
        ? {case: 'known', value: {source: OffersSource.PROBED}}
        : {case: 'unreadable', value: {reason: OffersUnreadableReason.UNREACHABLE}},
    }),
  );

/** The host's places in some order, each of a distinct name. */
const placesArb = fc
  .shuffledSubarray([...NAMES], {minLength: 0})
  .chain(names => fc.tuple(...names.map(placeArb)))
  .map(places => places as EvalPlace[]);

describe('the catalogue grouped by domain', () => {
  it('puts every entry in exactly one group, with no empty group', () => {
    fc.assert(
      fc.property(entriesArb, entries => {
        const groups = groupByDomain(entries);
        const listed = groups.flatMap(group => group.entries);
        expect(listed).toHaveLength(entries.length);
        expect(new Set(listed)).toEqual(new Set(entries));
        for (const group of groups) {
          expect(group.entries.length).toBeGreaterThan(0);
          for (const entry of group.entries) expect(domainOf(entry)).toBe(group.domain);
        }
        expect(new Set(groups.map(group => group.domain)).size).toBe(groups.length);
      }),
    );
  });

  it('orders the entries of a group by folder name, whatever order the host sent', () => {
    fc.assert(
      fc.property(entriesArb, fc.nat(), (entries, turn) => {
        const rotated = [...entries.slice(turn % (entries.length + 1)), ...entries];
        const again = groupByDomain(rotated.slice(0, entries.length));
        for (const group of [...groupByDomain(entries), ...again]) {
          const names = group.entries.map(folderName);
          expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b)));
        }
      }),
    );
  });

  it('lists a domain the client does not know last, with those the host left unnamed', () => {
    fc.assert(
      fc.property(entriesArb, entries => {
        const groups = groupByDomain(entries);
        const unspecified = groups.findIndex(group => group.domain === TaskDomain.UNSPECIFIED);
        if (unspecified >= 0) expect(unspecified).toBe(groups.length - 1);
      }),
    );
  });

  it('keeps two tasks of one example apart: each is its own entry with its own key', () => {
    const entry = (task: string) =>
      create(CatalogueEntrySchema, {
        example: 'data-structures/prioqueue',
        card: create(TaskCardSchema, {task, domain: TaskDomain.DATA_STRUCTURES}),
      });
    const entries = [entry('mpmc'), entry('spsc')];
    expect(new Set(entries.map(entryKey)).size).toBe(2);
    expect(groupByDomain(entries).flatMap(group => group.entries)).toHaveLength(2);
  });

  it('names an entry by the last part of its example id', () => {
    expect(folderName({example: 'model-serving/flint'})).toBe('flint');
    expect(folderName({example: 'flint'})).toBe('flint');
    expect(folderName({example: 'a/b/'})).toBe('b');
  });
});

describe('the catalogue filter', () => {
  it('only narrows: what it shows is a subset in the host’s order', () => {
    fc.assert(
      fc.property(entriesArb, fc.string({maxLength: 6}), (entries, query) => {
        const shown = visibleEntries(entries, {query});
        expect(shown.every(entry => entries.includes(entry))).toBe(true);
        expect(shown.map(entry => entries.indexOf(entry))).toEqual(
          shown.map(entry => entries.indexOf(entry)).sort((a, b) => a - b),
        );
      }),
    );
  });

  it('an empty query shows every example, including ones that do not fit this machine', () => {
    fc.assert(
      fc.property(entriesArb, entries => {
        expect(visibleEntries(entries, {query: '  '})).toEqual(entries);
      }),
    );
  });

  it('finds an entry by its own folder name', () => {
    fc.assert(
      fc.property(
        entriesArb.filter(entries => entries.length > 0),
        fc.nat(),
        (entries, at) => {
          const entry = entries[at % entries.length];
          if (entry === undefined) return;
          expect(visibleEntries(entries, {query: folderName(entry)})).toContain(entry);
        },
      ),
    );
  });

  it('the word is the host’s verdict and nothing else', () => {
    fc.assert(
      fc.property(fitArb, fit => {
        expect(fitWord(fit) === 'Fits').toBe(fit.state === FitState.FITS);
        expect(fitWord(fit) === 'Places unreadable').toBe(fit.state === FitState.PLACES_UNREADABLE);
      }),
    );
    expect(fitWord(undefined)).toBe('Does not fit');
  });
});

describe('the eval place pick', () => {
  it('is the first place in the host’s order that the fit names, or none', () => {
    fc.assert(
      fc.property(placesArb, fitArb, (places, fit) => {
        const picked = autoPick(places, fit);
        const fitting = places.filter(place => placeFits(place, fit));
        expect(picked).toBe(fitting[0]?.name);
      }),
    );
  });

  it('never picks a place the host did not name', () => {
    fc.assert(
      fc.property(
        placesArb,
        fitArb,
        fc.option(fc.constantFrom(...NAMES), {nil: null}),
        (places, fit, chosen) => {
          const picked = placeChoice(chosen, places, fit);
          if (picked !== undefined) {
            expect(fit.places).toContain(picked);
            expect(places.map(place => place.name)).toContain(picked);
          } else {
            expect(places.some(place => placeFits(place, fit))).toBe(false);
          }
        },
      ),
    );
  });

  it('keeps the person’s pick while it fits and falls back to the first fit when it stops fitting', () => {
    fc.assert(
      fc.property(placesArb, fitArb, fc.constantFrom(...NAMES), (places, fit, chosen) => {
        const stays = places.some(place => place.name === chosen && placeFits(place, fit));
        const picked = placeChoice(chosen, places, fit);
        expect(picked).toBe(stays ? chosen : autoPick(places, fit));
      }),
    );
  });

  it('says one word per place: unreadable, fits, or does not fit', () => {
    fc.assert(
      fc.property(placesArb, fitArb, (places, fit) => {
        for (const place of places) {
          const word = placeWord(place, fit);
          if (!readable(place)) expect(word).toBe('unreadable');
          else expect(word).toBe(placeFits(place, fit) ? 'fits' : 'does not fit');
        }
      }),
    );
  });
});

describe('the repository name of a copied example', () => {
  it('is always one the host accepts for a folder name that starts with a letter or digit', () => {
    fc.assert(
      fc.property(fc.stringMatching(/^[A-Za-z0-9][A-Za-z0-9._ -]{0,30}$/), folder => {
        expect(defaultName(folder)).toMatch(/^[a-z0-9][a-z0-9-]{0,62}$/);
      }),
    );
  });
});
