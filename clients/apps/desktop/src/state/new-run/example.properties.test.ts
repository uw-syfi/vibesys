import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  CatalogueEntrySchema,
  type EvalPlace,
  EvalPlaceKind,
  EvalPlaceSchema,
  type Fit,
  FitSchema,
  FitState,
  OffersSource,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {emptyExampleDraft, exampleTarget, fits, fitWord, pickExample} from './index.js';

const NAMES = ['a', 'b', 'c'] as const;

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

const placesArb: fc.Arbitrary<EvalPlace[]> = fc.shuffledSubarray([...NAMES]).map(names =>
  names.map(name =>
    create(EvalPlaceSchema, {
      name,
      kind: EvalPlaceKind.SSH,
      state: {case: 'known', value: {source: OffersSource.PROBED}},
    }),
  ),
);

const draftArb = fc.record({
  query: fc.constant(''),
  picked: fc.option(fc.constantFrom('model-serving/a:t', 'databases/b:t', 'x/missing:t'), {
    nil: null,
  }),
  place: fc.option(fc.constantFrom(...NAMES), {nil: null}),
  name: fc.option(fc.constantFrom('', '  ', 'copy-1'), {nil: null}),
  parent: fc.constantFrom('', ' ~/work ', '/srv/repos'),
});

const withExample = (example: string, fit: Fit) =>
  create(CatalogueEntrySchema, {example, card: create(TaskCardSchema, {task: 't'}), fit});

describe('what a Start from an example comes to', () => {
  it('is ready only for an example the host says fits, on a place it named, with a name', () => {
    fc.assert(
      fc.property(fitArb, fitArb, placesArb, draftArb, (fitA, fitB, places, draft) => {
        const entries = [withExample('model-serving/a', fitA), withExample('databases/b', fitB)];
        const target = exampleTarget(entries, places, draft);
        if (!target.ready) return;
        expect(fits(target.entry)).toBe(true);
        expect(target.entry.fit?.places).toContain(target.place);
        expect(target.name).not.toBe('');
        expect(target.parent).toBe(target.parent.trim());
      }),
    );
  });

  it('says the host’s word when the picked example does not fit, and never computes it', () => {
    fc.assert(
      fc.property(fitArb, placesArb, (fit, places) => {
        const entry = withExample('model-serving/a', fit);
        const target = exampleTarget([entry], places, {
          ...emptyExampleDraft,
          picked: 'model-serving/a:t',
        });
        if (!fits(entry)) expect(target).toEqual({ready: false, reason: fitWord(fit)});
      }),
    );
  });

  it('asks for an example before anything else', () => {
    expect(exampleTarget([], [], emptyExampleDraft)).toEqual({
      ready: false,
      reason: 'Pick an example',
    });
  });

  it('forgets the place and the name when another example is picked', () => {
    const picked = pickExample(
      {...emptyExampleDraft, place: 'a', name: 'mine', parent: '~/x'},
      'databases/b:t',
    );
    expect(picked).toMatchObject({picked: 'databases/b:t', place: null, name: null, parent: '~/x'});
  });
});
