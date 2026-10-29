import {describe, expect, test} from 'bun:test';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {
  allExamples,
  domainOptions,
  entriesFor,
  entriesOf,
  exampleLabel,
  hardwareGroups,
  hardwareKey,
  hasDetails,
  modelGroups,
} from './catalogue.js';

const ENTRIES = entriesOf(exampleCatalogue);

describe('the example catalogue as the questions read it', () => {
  test('there is one entry per task, none for an example that was not fetched', () => {
    const tasks = exampleCatalogue.examples.flatMap(example => example.task_details ?? []);
    expect(ENTRIES.length).toBe(tasks.length);
    expect(ENTRIES.some(entry => entry.example === 'model-serving/repositories/vllm')).toBe(false);
    expect(hasDetails(ENTRIES)).toBe(true);
  });

  test('a helper without the catalogue lists examples and their task names only', () => {
    const plain = entriesOf(exampleCatalogueWithoutDetails);
    expect(hasDetails(plain)).toBe(false);
    expect(plain.every(entry => entry.details === null)).toBe(true);
    expect(plain.map(entry => entry.task)).toContain('vllm');
    expect(allExamples(plain)).toContain('data-structures/stack');
  });

  test('the domains offered are the ones with an example, with how many', () => {
    const offered = domainOptions(ENTRIES);
    expect(offered.map(option => option.domain)).toEqual([
      'llm-serving',
      'data-structures',
      'databases',
      'microservices',
    ]);
    expect(offered.find(option => option.domain === 'data-structures')?.count).toBe(1);
  });

  test('models are grouped by maker, one option per distinct model whatever hardware it has examples for', () => {
    const groups = modelGroups(ENTRIES);
    const meta = groups.find(group => group.maker === 'meta');
    expect(meta?.models.map(model => model.name)).toContain('Llama 3 8B Instruct');
    const twice = entriesOf({
      ...exampleCatalogue,
      examples: exampleCatalogue.examples.flatMap(example => {
        const details = example.task_details;
        if (
          details === undefined ||
          details === null ||
          example.name !== 'model-serving/Llama-3-8B-trn2'
        ) {
          return [];
        }
        // The same model served on a second kind of hardware.
        return [
          example,
          {
            ...example,
            name: 'model-serving/Llama-3-8B-mi210',
            task_details: details.map(each => ({
              ...each,
              hardware: {
                accelerator: 'AMD MI210',
                count: 1,
                vendor: 'amd' as const,
                backend: 'rocm' as const,
              },
            })),
          },
        ];
      }),
    });
    const llama = modelGroups(twice).flatMap(group => group.models);
    expect(llama).toEqual([{name: 'Llama 3 8B Instruct'}]);
    // The two hardware are the model's next question, not the model's own card.
    expect(
      hardwareGroups(twice, 'Llama 3 8B Instruct').flatMap(group =>
        group.options.map(option => option.label),
      ),
    ).toEqual(['1\u00d7 AMD MI210', '1\u00d7 AWS Trainium2']);
    // Each model appears once, under one maker.
    const names = groups.flatMap(group => group.models.map(model => model.name));
    expect(new Set(names).size).toBe(names.length);
  });

  test('the hardware offered is the hardware the chosen model has an example for, once each, by vendor', () => {
    const everyKey = new Set(
      ENTRIES.flatMap(entry =>
        entry.details?.domain === 'llm-serving' ? [hardwareKey(entry.details.hardware)] : [],
      ),
    );
    const models = modelGroups(ENTRIES).flatMap(group => group.models.map(model => model.name));
    let narrower = false;
    for (const model of models) {
      const keys = hardwareGroups(ENTRIES, model).flatMap(group =>
        group.options.map(option => option.key),
      );
      expect(new Set(keys).size).toBe(keys.length);
      expect(keys.length).toBeGreaterThan(0);
      for (const key of everyKey) {
        expect(keys.includes(key)).toBe(entriesFor(ENTRIES, model, key).length > 0);
      }
      if (keys.length < everyKey.size) narrower = true;
    }
    expect(narrower).toBe(true);
    expect(hardwareGroups(ENTRIES, 'No such model')).toEqual([]);
  });

  test('an example is named by its own name, not its category directory', () => {
    expect(exampleLabel('data-structures/stack')).toBe('stack');
    expect(exampleLabel('stack')).toBe('stack');
  });
});
