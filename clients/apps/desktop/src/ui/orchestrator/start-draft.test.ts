import {createValidator} from '@bufbuild/protovalidate';
import {Harness, StartRunSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {startRunMessage} from './intents.js';
import {
  checkDraft,
  type DraftCheck,
  FANOUT_CHOICES,
  LEAD_CHOICES,
  LEADS,
  missingFrom,
  NO_STEERING,
  type StartDraft,
  type StartRunRequest,
  startRunRequest,
  TIME_LIMITS,
} from './start-draft.js';
import {type ModelChoices, NO_CATALOG} from './start-models.js';

/** The command a draft starts, or null when something keeps it from starting. */
const requestOf = (check: DraftCheck): StartRunRequest | null =>
  check._tag === 'Ready' ? check.request : null;

/** What the cost field says is wrong, or null when it reads. */
const costProblem = (check: DraftCheck): string | null =>
  check._tag === 'Ready'
    ? null
    : (check.problems.find(problem => problem.field === 'costLimit')?.message ?? null);

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

/** A draft that names its repository and task and has no cost, so only the cost text decides. */
const named: StartDraft = {
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

/** Dollars as a person writes them: digits, a point and cents or not, `$` or not, spaces around. */
const writtenDollars = fc
  .record({
    whole: fc.stringMatching(/^\d{0,7}$/),
    point: fc.boolean(),
    cents: fc.stringMatching(/^\d{0,4}$/),
    dollar: fc.constantFrom('', '$', '$ '),
    before: fc.constantFrom('', ' ', '\t'),
    after: fc.constantFrom('', ' ', '\n'),
  })
  .filter(({whole, point, cents}) => whole !== '' || (point && cents !== ''));

/**
 * Text with at least one character a decimal amount of dollars never has: a letter, a comma, an
 * underscore, a plus, a digit of another script. `0x10`, `1e3`, `0b11` and `0o7` are all of these.
 */
const notDollars = fc.oneof(
  fc.constantFrom('0x10', '1e3', '0b11', '0o7', 'Infinity', '1_000', '+5', '1,000', '١٢'),
  fc
    .tuple(
      fc.stringMatching(/^[\d.$ -]{0,4}$/),
      fc.oneof(
        fc.constantFrom('e', 'E', 'x', 'b', 'o', ',', '_', '+'),
        fc.string({unit: 'grapheme', minLength: 1, maxLength: 1}),
      ),
      fc.stringMatching(/^[\d.$ -]{0,4}$/),
    )
    .filter(([, odd]) => !/^[\d.$\s-]$/.test(odd))
    .map(parts => parts.join('')),
);

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
  it('is valid on the wire whenever the form can start', () => {
    const validator = createValidator();
    fc.assert(
      fc.property(draft, choicesOf, (value, choices) => {
        const request = requestOf(checkDraft(value, choices));
        if (request === null) return;
        const message = startRunMessage(request);
        expect(validator.validate(StartRunSchema, message).kind).toBe('valid');
      }),
    );
  });

  it('carries the steering text as the brief, and a fixed line when there is none', () => {
    fc.assert(
      fc.property(draft, choicesOf, (value, choices) => {
        const request = requestOf(checkDraft(value, choices));
        if (request === null) return;
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
        const request = requestOf(checkDraft(value, choices));
        if (request === null) return;
        const {model} = request.lead;
        if (choices.pinned && choices.models.length > 0) expect(choices.models).toContain(model);
        else if (!choices.pinned) expect(model === '' || choices.models.includes(model)).toBe(true);
      }),
    );
  });

  it('carries the time limit in seconds and the cost limit in dollars, or none', () => {
    fc.assert(
      fc.property(draft, value => {
        const request = requestOf(checkDraft(value, NO_CATALOG));
        if (request === null) return;
        expect(request.maxTimeSeconds).toBe(value.timeLimit === null ? null : value.timeLimit * 60);
        expect(request.maxCostUsd === null).toBe(value.costLimit.trim() === '');
        if (request.maxCostUsd !== null) expect(request.maxCostUsd).toBeGreaterThan(0);
      }),
    );
  });

  it('reads a cost written in dollars as that amount, `$` and spaces aside', () => {
    fc.assert(
      fc.property(writtenDollars, written => {
        const {whole, point, cents, dollar, before, after} = written;
        const digits = `${whole}${point ? '.' : ''}${point ? cents : ''}`;
        const amount = Number(digits);
        const check = checkDraft(
          {...named, costLimit: `${before}${dollar}${digits}${after}`},
          NO_CATALOG,
        );
        if (amount === 0) {
          expect(costProblem(check)).toBe(
            'The cost limit must be more than zero. Leave it empty for no limit.',
          );
          return;
        }
        expect(requestOf(check)?.maxCostUsd).toBe(amount);
      }),
    );
  });

  it('refuses a cost with anything but digits, a point and `$`, and never starts on it', () => {
    fc.assert(
      fc.property(notDollars, text => {
        const check = checkDraft({...named, costLimit: text}, NO_CATALOG);
        expect(check._tag).toBe('NotReady');
        expect(costProblem(check)).toMatch(/^The cost limit /);
      }),
    );
  });

  it('says what is wrong with a cost in words that fit it', () => {
    const said = (costLimit: string) => costProblem(checkDraft({...named, costLimit}, NO_CATALOG));
    for (const text of ['0', '0.00', '$0', '-5', '- $5', '-0.5'])
      expect(said(text)).toBe(
        'The cost limit must be more than zero. Leave it empty for no limit.',
      );
    for (const text of ['1,000', '12,50'])
      expect(said(text)).toBe('The cost limit is written without commas, such as 1000 or 12.50.');
    for (const text of ['lots', '1e3', '0x10', '$', '.', '5$', '1 000'])
      expect(said(text)).toBe('The cost limit is a number of dollars, such as 25 or 12.50.');
    expect(said(`1${'0'.repeat(400)}`)).toBe('The cost limit is too large.');
    for (const text of ['', '  ', '25', '$25', '$ 12.50', '.5', '7.'])
      expect(said(text)).toBeNull();
  });

  it('asks for a repository and a task, and a cost limit that is an amount', () => {
    const ok = named;
    expect(missingFrom(ok, NO_CATALOG)).toBeNull();
    expect(missingFrom({...ok, repository: '  '}, NO_CATALOG)).toContain('repository');
    expect(missingFrom({...ok, task: ''}, NO_CATALOG)).toContain('task');
    expect(missingFrom({...ok, costLimit: '5'}, NO_CATALOG)).toBeNull();
    for (const bad of ['0', '-1', 'abc', 'Infinity'])
      expect(missingFrom({...ok, costLimit: bad}, NO_CATALOG)).toContain('cost limit');
  });

  it('cannot start when the host allows no model for the tool', () => {
    const blocked: ModelChoices = {models: [], pinned: true, blocked: 'No allowed model.'};
    expect(missingFrom(named, blocked)).toBe('No allowed model.');
  });

  it('names the field of every problem, in the order the form shows them', () => {
    const check = checkDraft(
      {...named, repository: ' ', task: '', costLimit: 'lots'},
      {models: [], pinned: true, blocked: 'No allowed model.'},
    );
    expect(check._tag === 'NotReady' && check.problems.map(problem => problem.field)).toEqual([
      'repository',
      'task',
      'model',
      'costLimit',
    ]);
  });

  it('leaves the repository and task unchecked for an example, but still blocks on the model and cost', () => {
    const fromExample: StartDraft = {...named, repository: '', task: ''};
    expect(missingFrom(fromExample, NO_CATALOG, 'example')).toBeNull();
    expect(checkDraft(fromExample, NO_CATALOG, 'example')._tag).toBe('Ready');
    const blocked: ModelChoices = {models: [], pinned: true, blocked: 'No allowed model.'};
    expect(missingFrom(fromExample, blocked, 'example')).toBe('No allowed model.');
  });

  it('carries the eval place through, and agrees with checkDraft’s own request', () => {
    fc.assert(
      fc.property(draft, choicesOf, (value, choices) => {
        const request = startRunRequest(value, choices);
        expect(request.evalPlace).toBe(value.evalPlace);
        const check = checkDraft(value, choices);
        if (check._tag === 'Ready') expect(check.request).toEqual(request);
      }),
    );
  });
});
