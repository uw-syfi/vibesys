import {create} from '@bufbuild/protobuf';
import {Harness, HarnessModelsSchema, ModelCatalogSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {type ModelChoices, modelChoices, NO_CATALOG, startModel} from './start-models.js';

const MODELS = ['model-a', 'model-b', 'model-c', 'model-d', 'model-e'];
const known = fc.record({
  codex: fc.uniqueArray(fc.constantFrom(...MODELS), {maxLength: 3}),
  claude: fc.uniqueArray(fc.constantFrom(...MODELS), {maxLength: 3}),
});
const catalogOf = fc.record({
  allowed: fc.uniqueArray(fc.constantFrom(...MODELS), {maxLength: 5}),
  unreadable: fc.boolean(),
  known,
  harness: fc.constantFrom(Harness.CODEX, Harness.CLAUDE_CODE),
});

type Draw = typeof catalogOf extends fc.Arbitrary<infer T> ? T : never;

const build = ({allowed, unreadable, known: entries}: Draw) =>
  create(ModelCatalogSchema, {
    allowedModels: allowed,
    unreadablePolicy: unreadable,
    harnesses: [
      create(HarnessModelsSchema, {harness: Harness.CODEX, models: entries.codex}),
      create(HarnessModelsSchema, {harness: Harness.CLAUDE_CODE, models: entries.claude}),
    ],
  });

function expectOfferedFrom(order: readonly string[], choices: ModelChoices): void {
  expect(choices.models.every(model => order.includes(model))).toBe(true);
  expect(choices.models).toEqual(order.filter(model => choices.models.includes(model)));
  expect(choices.blocked === null).toBe(choices.models.length > 0);
  if (choices.blocked !== null) return;
  // Whatever was chosen, a host that allows some is never left to the tool's default.
  for (const chosen of ['', 'model-z', ...MODELS])
    expect(order).toContain(startModel(chosen, choices));
  expect(startModel('', choices)).toBe(choices.models[0]);
}

describe('the models the Start run form offers', () => {
  it('are, on a host that allows some, only allowed ones, in the host’s order, and the first is the start', () => {
    fc.assert(
      fc.property(catalogOf, value => {
        const catalog = build(value);
        const choices = modelChoices(catalog, value.harness);
        if (catalog.unreadablePolicy) {
          expect(choices.blocked).not.toBeNull();
          expect(choices.models).toEqual([]);
          return;
        }
        if (catalog.allowedModels.length === 0) {
          expect(choices.pinned).toBe(false);
          expect(startModel('', choices)).toBe('');
          return;
        }
        expect(choices.pinned).toBe(true);
        expectOfferedFrom(catalog.allowedModels, choices);
      }),
    );
  });

  it('never offer a tool a model that only another tool is known to run', () => {
    fc.assert(
      fc.property(catalogOf, value => {
        const catalog = build(value);
        if (catalog.unreadablePolicy || catalog.allowedModels.length === 0) return;
        const other = value.harness === Harness.CODEX ? value.known.claude : value.known.codex;
        const own = value.harness === Harness.CODEX ? value.known.codex : value.known.claude;
        for (const model of modelChoices(catalog, value.harness).models) {
          expect(own.includes(model) || !other.includes(model)).toBe(true);
        }
      }),
    );
  });

  it('are the tool’s own models, with the tool’s default left as a choice, on a host with no policy', () => {
    const catalog = create(ModelCatalogSchema, {
      harnesses: [
        create(HarnessModelsSchema, {harness: Harness.CODEX, models: ['model-a', 'model-b']}),
      ],
    });
    const choices = modelChoices(catalog, Harness.CODEX);
    expect(choices).toEqual({models: ['model-a', 'model-b'], pinned: false, blocked: null});
    expect(startModel('', choices)).toBe('');
    expect(startModel('model-b', choices)).toBe('model-b');
    expect(startModel('model-z', choices)).toBe('');
  });

  it('are the tool’s default until the host has said anything', () => {
    expect(modelChoices(null, Harness.CODEX)).toEqual(NO_CATALOG);
  });
});
