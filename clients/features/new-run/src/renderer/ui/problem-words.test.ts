import {expect, test} from 'bun:test';
import {FAILURE_CAUSE_WORDS, type FailureCause, UNEXPECTED_FAILURE} from '@vibesys/ipc';
import {type Problem, problemOf, type Step, StepFailure} from '../../state/index.js';
import {MAY_HAVE_STARTED, problemWords} from './problem-words.js';

/** Every step, as a closed set: a step added without words is a compile error here. */
const STEPS: Readonly<Record<Step, true>> = {
  features: true,
  describe: true,
  harnesses: true,
  examples: true,
  tasks: true,
  goal: true,
  preflight: true,
  fix: true,
  start: true,
};
const ALL_STEPS = Object.keys(STEPS) as Step[];

const RAW_ERRORS: readonly unknown[] = [
  new TypeError('bridge.harnesses is not a function'),
  new Error("Cannot read properties of undefined (reading 'kind')"),
  'Error invoking remote method',
  undefined,
  42,
];

test('a failure nobody reported never shows its raw error, for any step', () => {
  for (const step of ALL_STEPS) {
    for (const raw of RAW_ERRORS) {
      const words = problemWords(problemOf(step, raw), 'host-a');
      const shown = `${words.title} ${words.body}`;
      expect(shown).not.toMatch(
        /is not a function|properties of undefined|remote method|undefined/,
      );
      expect(words.title).toEndWith('.');
      expect(words.body.length).toBeGreaterThan(0);
      // The status is a few words, never a sentence: the sentence is its description.
      expect(words.label).not.toMatch(/[.!?]/);
      expect(words.label.split(' ').length).toBeLessThanOrEqual(3);
    }
  }
});

const CAUSES: readonly FailureCause[] = [
  'link',
  'auth',
  'incompatible',
  'run-gone',
  'denied',
  'capacity',
];
const RAW =
  "the host helper for /srv/p/x on host-a exited with 1: /srv/.venv/bin/python3: Error (ModuleNotFoundError: No module named 'entrypoints')";

test('a connection failure shows a status word, and keeps its raw text for the tooltip', () => {
  for (const step of ALL_STEPS) {
    for (const cause of CAUSES) {
      const words = problemWords({step, detail: RAW, cause}, 'host-a');
      expect(words.body).toBe(FAILURE_CAUSE_WORDS[cause]);
      expect(words.detail).toBe(RAW);
      expect(`${words.title} ${words.body}`).not.toMatch(/\/srv|exited with|Error/);
    }
  }
});

test('a failure the main process worded is shown as worded, with no tooltip to add', () => {
  for (const step of ALL_STEPS) {
    const words = problemWords({step, detail: 'No such project.', cause: null}, 'host-a');
    expect(words.body).toBe('No such project.');
    expect(words.detail).toBeNull();
  }
});

test('a reported failure keeps the words the main process gave it', () => {
  for (const step of ALL_STEPS) {
    const problem = problemOf(step, new StepFailure('host-a refused the connection.'));
    expect(problemWords(problem, 'host-a').body).toBe('host-a refused the connection.');
  }
});

test('every title names what failed, and the machine where the step is about it', () => {
  const titles = ALL_STEPS.map(
    step => problemWords({step, detail: null, cause: null}, 'host-a').title,
  );
  expect(new Set(titles).size).toBe(ALL_STEPS.length);
  for (const step of ['features', 'describe', 'harnesses', 'examples'] as const) {
    expect(problemWords({step, detail: null, cause: null}, 'host-a').title).toContain('host-a');
  }
});

test('a load or check can be tried again from the banner; a fix or start is pressed again', () => {
  const retry = (step: Step) => problemWords({step, detail: null, cause: null}, 'host-a').retry;
  const retried: Step[] = [
    'describe',
    'examples',
    'features',
    'goal',
    'harnesses',
    'preflight',
    'tasks',
  ];
  expect(ALL_STEPS.filter(retry).sort()).toEqual(retried);
});

test('an unreported start does not claim that nothing started', () => {
  const unreported: Problem = {step: 'start', detail: null, cause: null};
  expect(problemWords(unreported, 'host-a').body).toContain('may have started');
  expect(problemWords({step: 'tasks', detail: null, cause: null}, 'host-a').body).toBe(
    UNEXPECTED_FAILURE,
  );
});

/** Every problem a step can have: nothing reported, a reported text, and a typed cause. */
function problemsOf(step: Step): Problem[] {
  return [
    {step, detail: null, cause: null},
    {step, detail: 'No such project.', cause: null},
    ...CAUSES.map(cause => ({step, detail: RAW, cause})),
  ];
}

test('property: only a start nobody reported on says it may have started, and sends to Runs', () => {
  for (const problem of ALL_STEPS.flatMap(problemsOf)) {
    const words = problemWords(problem, 'host-a');
    const unconfirmed = problem.step === 'start' && problem.detail === null;
    expect(words.look, JSON.stringify(problem)).toBe(unconfirmed);
    // A start that failed for a known reason is worded differently from one that may have happened.
    expect(words.label === MAY_HAVE_STARTED, JSON.stringify(problem)).toBe(unconfirmed);
  }
});
