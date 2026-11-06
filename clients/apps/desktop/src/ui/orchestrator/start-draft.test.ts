import {create} from '@bufbuild/protobuf';
import {createValidator} from '@bufbuild/protovalidate';
import {Harness, StartRunSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {
  FANOUT_CHOICES,
  LEAD_CHOICES,
  LEADS,
  missingFrom,
  NO_STEERING,
  type StartDraft,
  startRunRequest,
} from './start-draft.js';

const draft: fc.Arbitrary<StartDraft> = fc.record({
  repository: fc.string(),
  task: fc.string(),
  fanout: fc.constantFrom(...FANOUT_CHOICES),
  lead: fc.constantFrom(...LEAD_CHOICES),
  steering: fc.string(),
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
      fc.property(draft, value => {
        if (missingFrom(value) !== null) return;
        const message = create(StartRunSchema, startRunRequest(value));
        expect(validator.validate(StartRunSchema, message).kind).toBe('valid');
      }),
    );
  });

  it('carries the steering text as the brief, and a fixed line when there is none', () => {
    fc.assert(
      fc.property(draft, value => {
        const request = startRunRequest(value);
        expect(request.brief).toBe(
          value.steering.trim() === '' ? NO_STEERING : value.steering.trim(),
        );
        expect(request.fanoutLimit).toBe(value.fanout);
      }),
    );
  });

  it('asks for a repository and a task, and nothing else', () => {
    const ok: StartDraft = {repository: '/r', task: 't', fanout: 3, lead: 'codex', steering: ''};
    expect(missingFrom(ok)).toBeNull();
    expect(missingFrom({...ok, repository: '  '})).toContain('repository');
    expect(missingFrom({...ok, task: ''})).toContain('task');
  });
});
