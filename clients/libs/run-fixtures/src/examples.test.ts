import {describe, expect, it} from 'bun:test';
import {parseHelperMessage} from '@vibesys/contracts/host-helper';
import {exampleCatalogue, exampleCatalogueWithoutDetails} from './examples.js';

function reply(listing: unknown): string {
  return JSON.stringify({type: 'examples.list', request_id: 'r1', listing});
}

describe('the example catalogue fixture', () => {
  it('is what a helper that offers example_catalogue may send', () => {
    expect(() => parseHelperMessage(reply(exampleCatalogue))).not.toThrow();
  });

  it('is what a helper without example_catalogue sends, with no task details at all', () => {
    expect(() => parseHelperMessage(reply(exampleCatalogueWithoutDetails))).not.toThrow();
    for (const example of exampleCatalogueWithoutDetails.examples) {
      expect('task_details' in example).toBe(false);
    }
  });

  it('names a task for every task a listed example has, and a model for each served one', () => {
    for (const example of exampleCatalogue.examples) {
      const detailed = (example.task_details ?? []).map(detail => detail.task);
      expect(detailed).toEqual(example.tasks);
      for (const detail of example.task_details ?? []) {
        expect(detail.model !== null).toBe(detail.domain === 'llm-serving');
        expect(detail.workload !== null).toBe(detail.domain === 'data-structures');
      }
    }
  });

  it('covers every domain, vendor and maker the contract names', () => {
    const details = exampleCatalogue.examples.flatMap(example => example.task_details ?? []);
    expect(new Set(details.map(detail => detail.domain))).toEqual(
      new Set(['llm-serving', 'data-structures', 'databases', 'microservices']),
    );
    expect(new Set(details.map(detail => detail.hardware.vendor))).toEqual(
      new Set(['nvidia', 'amd', 'aws', 'apple', 'cpu']),
    );
    expect(new Set(details.flatMap(detail => (detail.model ? [detail.model.maker] : [])))).toEqual(
      new Set(['meta', 'qwen', 'deepseek', 'zhipu', 'moonshot', 'ai2', 'openai']),
    );
  });
});
