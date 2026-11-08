import {createValidator} from '@bufbuild/protovalidate';
import {Harness, StartRunSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {startRunMessage} from './intents.js';
import {
  FANOUT_CHOICES,
  LEAD_CHOICES,
  LEADS,
  missingFrom,
  NO_STEERING,
  type StartDraft,
  startRunRequest,
  TIME_LIMITS,
} from './start-draft.js';
import {type ModelChoices, NO_CATALOG} from './start-models.js';

const choicesOf: fc.Arbitrary<ModelChoices> = fc.oneof(
  fc.constant(NO_CATALOG),
  fc.record({
    models: fc.uniqueArray(fc.constantFrom('model-a', 'model-b', 'model-c'), {minLength: 1}),
    pinned: fc.boolean(),
    blocked: fc.constant(null),
  }),
  fc.constant({models: [], pinned: true, blocked: 'No allowed model runs on this tool.'}),
);

const draft: fc.Arbitrary<StartDraft> = fc.record({
  repository: fc.string(),
  task: fc.string(),
  fanout: fc.constantFrom(...FANOUT_CHOICES),
  lead: fc.constantFrom(...LEAD_CHOICES),
  model: fc.constantFrom('', 'model-a', 'model-b', 'model-z'),
  timeLimit: fc.constantFrom(...TIME_LIMITS),
  costLimit: fc.oneof(
    fc.constant(''),
    fc.double({min: -10, max: 1000, noNaN: true}).map(String),
    fc.string(),
  ),
  steering: fc.string(),
  evalPlace: fc.constantFrom('', 'host-a'),
});

describe('the lead choices', () => {
  it('are every harness of the enum but the unspecified one, spelled in lower case', () => {
    const named = Object.values(Harness).filter(
      (value): value is Harness => typeof value === 'number' && value !== Harness.UNSPECIFIED,
    );
    expect(LEAD_CHOICES.map(choice => LEADS[choice]).sort()).toEqual([...named].sort());
    for (const choice of LEAD_CHOICES) expect(Harness[LEADS[choice]].toLowerCase()).toBe(choice);
  });
});

describe('the Start run form’s command', () => {
  it('is valid on the wire exactly when the form says nothing is missing', () => {
    const validator = createValidator();
    fc.assert(
      fc.property(draft, choicesOf, (value, choices) => {
        if (missingFrom(value, choices) !== null) return;
        const message = startRunMessage(startRunRequest(value, choices));
        expect(validator.validate(StartRunSchema, message).kind).toBe('valid');
      }),
    );
  });

  it('carries the steering text as the brief, and a fixed line when there is none', () => {
    fc.assert(
      fc.property(draft, choicesOf, (value, choices) => {
        const request = startRunRequest(value, choices);
        expect(request.brief).toBe(
          value.steering.trim() === '' ? NO_STEERING : value.steering.trim(),
        );
        expect(request.fanoutLimit).toBe(value.fanout);
      }),
    );
  });

  it('names the model explicitly on a host that allows only some, and never one it did not offer', () => {
    fc.assert(
      fc.property(draft, choicesOf, (value, choices) => {
        const {model} = startRunRequest(value, choices).lead;
        if (choices.pinned && choices.models.length > 0) expect(choices.models).toContain(model);
        else if (!choices.pinned) expect(model === '' || choices.models.includes(model)).toBe(true);
      }),
    );
  });

  it('carries the time limit in seconds and the cost limit in dollars, or none', () => {
    fc.assert(
      fc.property(draft, value => {
        if (missingFrom(value, NO_CATALOG) !== null) return;
        const request = startRunRequest(value, NO_CATALOG);
        expect(request.maxTimeSeconds).toBe(value.timeLimit === null ? null : value.timeLimit * 60);
        if (request.maxCostUsd !== null) expect(request.maxCostUsd).toBeGreaterThan(0);
      }),
    );
  });

  it('asks for a repository and a task, and a cost limit that is an amount', () => {
    const ok: StartDraft = {
      repository: '/r',
      task: 't',
      fanout: 3,
      lead: 'codex',
      model: '',
      timeLimit: null,
      costLimit: '',
      steering: '',
      evalPlace: '',
    };
    expect(missingFrom(ok, NO_CATALOG)).toBeNull();
    expect(missingFrom({...ok, repository: '  '}, NO_CATALOG)).toContain('repository');
    expect(missingFrom({...ok, task: ''}, NO_CATALOG)).toContain('task');
    expect(missingFrom({...ok, costLimit: '5'}, NO_CATALOG)).toBeNull();
    for (const bad of ['0', '-1', 'abc', 'Infinity'])
      expect(missingFrom({...ok, costLimit: bad}, NO_CATALOG)).toContain('cost limit');
  });

  it('cannot start when the host allows no model for the tool', () => {
    const blocked: ModelChoices = {models: [], pinned: true, blocked: 'No allowed model.'};
    const ok = {
      repository: '/r',
      task: 't',
      fanout: 3,
      lead: 'codex',
      model: '',
      timeLimit: null,
      costLimit: '',
      steering: '',
      evalPlace: '',
    } satisfies StartDraft;
    expect(missingFrom(ok, blocked)).toBe('No allowed model.');
  });
});
