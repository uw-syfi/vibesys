import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  CatalogueEntrySchema,
  type EvalPlace,
  EvalPlaceKind,
  EvalPlaceSchema,
  FitSchema,
  FitState,
  OffersSource,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  firstPage,
  fitsHere,
  machineFacts,
  type NewRunPage,
  offeredPlaces,
  type PageAction,
  pageReducer,
  placesFor,
  variantOf,
} from './index.js';

const KINDS = [
  EvalPlaceKind.LOCAL,
  EvalPlaceKind.SSH,
  EvalPlaceKind.SLURM,
  EvalPlaceKind.CLOUD,
] as const;
const NAMES = ['a', 'b', 'c', 'd'] as const;

const placeOf = (name: string, kind: EvalPlaceKind): EvalPlace =>
  create(EvalPlaceSchema, {
    name,
    kind,
    state: {case: 'known', value: {source: OffersSource.PROBED}},
  });

const placesArb = fc
  .uniqueArray(fc.constantFrom(...NAMES))
  .chain(names =>
    fc
      .array(fc.constantFrom(...KINDS), {minLength: names.length, maxLength: names.length})
      .map(kinds => names.map((name, index) => placeOf(name, kinds[index] ?? EvalPlaceKind.SSH))),
  );

const entryOf = (state: FitState, places: string[], task = 't', example = 'x/ex') =>
  create(CatalogueEntrySchema, {
    example,
    card: create(TaskCardSchema, {task}),
    fit: create(FitSchema, {state, places}),
  });

const stepArb = fc.constantFrom('start', 'catalog', 'own' as const);
const actionArb: fc.Arbitrary<PageAction> = fc.oneof(
  stepArb.map(step => ({type: 'open', step}) as const),
  fc.constantFrom('tasks', 'repo', 'none' as const).map(own => ({type: 'own', own}) as const),
  fc.option(fc.string(), {nil: null}).map(task => ({type: 'own-task', task}) as const),
  fc.string().map(name => ({type: 'new-name', name}) as const),
  fc.constant({type: 'machine'} as const),
  fc.constant({type: 'back'} as const),
);

describe('the places the page offers', () => {
  it('are never a cloud place, and keep the host’s order of the rest', () => {
    fc.assert(
      fc.property(placesArb, places => {
        const offered = offeredPlaces(places);
        expect(offered.some(place => place.kind === EvalPlaceKind.CLOUD)).toBe(false);
        expect(offered.map(place => place.name)).toEqual(
          places.filter(place => place.kind !== EvalPlaceKind.CLOUD).map(place => place.name),
        );
      }),
    );
  });

  it('fit an entry only when the host says it fits and names an offered place', () => {
    fc.assert(
      fc.property(
        placesArb,
        fc.constantFrom(FitState.FITS, FitState.NO_ACCELERATOR, FitState.PLACES_UNREADABLE),
        fc.uniqueArray(fc.constantFrom(...NAMES)),
        (places, state, named) => {
          const entry = entryOf(state, named);
          const offered = new Set(offeredPlaces(places).map(place => place.name));
          const expected = state === FitState.FITS && named.some(name => offered.has(name));
          expect(fitsHere(entry, places)).toBe(expected);
          for (const place of placesFor(entry, places)) {
            expect(offered.has(place.name)).toBe(true);
            expect(named).toContain(place.name);
          }
        },
      ),
    );
  });
});

describe('the machine strip’s figures', () => {
  it('count only what was read, and never more fits than examples', () => {
    fc.assert(
      fc.property(
        placesArb,
        fc.array(
          fc.record({fits: fc.boolean(), named: fc.uniqueArray(fc.constantFrom(...NAMES))}),
          {maxLength: 8},
        ),
        fc.array(fc.record({isGitRepository: fc.boolean(), hasTasks: fc.boolean()})),
        (places, shapes, folder) => {
          const entries = shapes.map(({fits, named}) =>
            entryOf(fits ? FitState.FITS : FitState.NO_ACCELERATOR, named),
          );
          const facts = machineFacts(places, entries, folder);
          expect(facts.places).toBe(offeredPlaces(places).length);
          expect(facts.examples).toBe(entries.length);
          expect(facts.fitting ?? 0).toBeLessThanOrEqual(entries.length);
          expect(facts.repositories).toBe(folder.filter(each => each.isGitRepository).length);
          expect(facts.withTasks).toBe(folder.filter(each => each.hasTasks).length);
          const unread = machineFacts(null, null, null);
          expect(Object.values(unread).every(value => value === null)).toBe(true);
        },
      ),
    );
  });
});

describe('the variant tag', () => {
  const variant = (task: string, example: string, fields: {modelName?: string} = {}) =>
    variantOf(
      create(CatalogueEntrySchema, {example, card: create(TaskCardSchema, {task, ...fields})}),
    );

  it('is what the task name says beyond the example, the hardware and default', () => {
    expect(variant('mi300x-agentx', 'model-serving/qwen3.6-35b-a3b')).toBe('agentx');
    expect(variant('from-scratch', 'model-serving/olmo-hybrid')).toBe('from scratch');
    expect(variant('vllm', 'model-serving/moonshine-streaming')).toBe('vllm');
  });

  it('is none for default, a hardware name, or a task named for its example or subject', () => {
    expect(variant('default', 'model-serving/qwen3-32b')).toBeUndefined();
    expect(variant('mi2104x', 'model-serving/flint')).toBeUndefined();
    expect(variant('flint', 'model-serving/flint')).toBeUndefined();
    expect(
      variant('range-heavy', 'data-structures/ordered-map', {modelName: 'Range heavy'}),
    ).toBeUndefined();
  });

  it('never repeats a word of the example folder', () => {
    fc.assert(
      fc.property(
        fc.array(fc.constantFrom('flint', 'agentx', 'vllm', 'default', 'mi300x', 'tracelab'), {
          minLength: 1,
          maxLength: 4,
        }),
        fc.constantFrom('flint', 'qwen-serve', 'agentx'),
        (words, folder) => {
          const tag = variant(words.join('-'), `model-serving/${folder}`);
          for (const word of tag?.split(' ') ?? []) {
            expect(folder.split('-')).not.toContain(word);
            expect(['default', 'mi300x']).not.toContain(word);
          }
        },
      ),
    );
  });
});

describe('the page’s steps', () => {
  const walk = (actions: readonly PageAction[]): NewRunPage =>
    actions.reduce(pageReducer, firstPage);

  it('start on the entry cards, and back from any step is the entry cards', () => {
    expect(firstPage.step).toBe('start');
    fc.assert(
      fc.property(fc.array(actionArb), actions => {
        expect(pageReducer(walk(actions), {type: 'back'}).step).toBe(
          walk(actions).step === 'start' ? 'start' : 'start',
        );
      }),
    );
  });

  it('forget a task picked when the case, or the machine, changes', () => {
    fc.assert(
      fc.property(fc.array(actionArb), actions => {
        const picked = walk([...actions, {type: 'own-task', task: 'decode'}]);
        expect(pageReducer(picked, {type: 'machine'}).ownTask).toBeNull();
        expect(pageReducer(picked, {type: 'own', own: 'repo'}).ownTask).toBeNull();
      }),
    );
  });

  it('keep the name typed across steps', () => {
    fc.assert(
      fc.property(fc.string(), stepArb, (name, step) => {
        const page = walk([{type: 'new-name', name}, {type: 'open', step}, {type: 'back'}]);
        expect(page.newName).toBe(step === 'own' ? '' : name);
      }),
    );
  });
});
