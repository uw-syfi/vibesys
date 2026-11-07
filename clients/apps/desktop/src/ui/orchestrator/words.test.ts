import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {ReceiptSchema, Refusal} from '@vibesys/orchestrator-proto';
import * as fc from 'fast-check';
import {receiptRefusalWord, refusalWord} from './words.js';

const modelId = fc.stringMatching(/^[a-z][a-z0-9.-]{0,15}$/);

const refused = (model: string, allowedModels: readonly string[]) =>
  create(ReceiptSchema, {
    commandId: 'c1',
    refusal: Refusal.MODEL_NOT_ALLOWED,
    model,
    allowedModels: [...allowedModels],
  });

describe('the words of a refusal', () => {
  it('name a refused model and every model the host allows', () => {
    fc.assert(
      fc.property(
        modelId,
        fc.uniqueArray(modelId, {minLength: 1, maxLength: 5}),
        (model, allowed) => {
          const words = receiptRefusalWord(refused(model, allowed));
          expect(words).toContain(model);
          for (const id of allowed) expect(words).toContain(id);
        },
      ),
    );
  });

  it('say there is no model for an unnamed one, and list what is allowed', () => {
    const words = receiptRefusalWord(refused('', ['model-a', 'model-b']));
    expect(words).toContain('model-a, model-b');
    expect(words).not.toContain('not allowed');
  });

  it('say the host cannot be read when the host lists nothing', () => {
    expect(receiptRefusalWord(refused('model-a', []))).toContain('unreadable');
  });

  it('leave every other refusal to its own word', () => {
    for (const refusal of [
      Refusal.RUN_NOT_FOUND,
      Refusal.REPOSITORY_IN_USE,
      Refusal.FANOUT_LIMIT,
    ]) {
      expect(receiptRefusalWord(create(ReceiptSchema, {commandId: 'c1', refusal}))).toBe(
        refusalWord(refusal),
      );
    }
  });
});
