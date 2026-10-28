/**
 * Driving New run: choosing an answer, going on, and opening a page directly. A page is opened by
 * giving the window the draft that would have brought it there (the draft is the window's own
 * storage, which the page reads as it starts), so a check of every page does not click through the
 * pages before it.
 */
import {expect, type Locator, type Page} from '@playwright/test';
import {
  type AgentChoice,
  type Answers,
  DRAFT_KEY,
  type Draft,
  NO_ANSWERS,
  type StepId,
  storeDraft,
} from '@vibesys/feature-new-run/state';
import type {FakeBridgeSeed} from './fake-bridge.js';
import {openNewRun, SEED} from './world.js';

/** The pages of every path, each with the answers that make it the page shown. */
export interface NewRunPage {
  readonly name: string;
  readonly step: StepId;
  readonly answers: Answers;
  /** The run's agent, when the page is shown with one chosen (else the machine's first). */
  readonly agent?: AgentChoice;
  /** The machine the form is for, when not the world's own. */
  readonly machine?: string;
  /** What the machine says of each harness CLI's login, when it says anything. */
  readonly logins?: Readonly<Record<string, string>>;
}

const EXAMPLES: Answers = {...NO_ANSWERS, from: 'example'};
const LLM: Answers = {
  ...EXAMPLES,
  entry: {example: 'model-serving/qwen3.5-9b-mi210', task: 'default'},
};
const STRUCTURE: Answers = {
  ...EXAMPLES,
  entry: {example: 'data-structures/stack', task: 'mpmc'},
};
const DATABASE: Answers = {...EXAMPLES, entry: {example: 'database/kv-store', task: 'default'}};
const PROJECTS: Answers = {...NO_ANSWERS, from: 'project'};
const OWN: Answers = {
  ...PROJECTS,
  project: {source: 'recent', path: '/home/me/projects/queue-rs'},
  task: 'mpmc',
};

/** The run's agent with the judge on another model of the run's harness. */
const JUDGE_ON_SONNET: AgentChoice = {
  provider: 'claude',
  model: 'claude-opus-4-5',
  roles: [
    {role: 'multi-orchestrator', agent: null, effort: 'default'},
    {role: 'multi-profiler', agent: null, effort: 'default'},
    {role: 'multi-implementer', agent: null, effort: 'default'},
    {role: 'multi-judge', agent: {provider: 'claude', model: 'claude-sonnet-4-5'}, effort: 'high'},
  ],
};

export const NEW_RUN_PAGES: readonly NewRunPage[] = [
  {name: 'pick', step: 'pick', answers: NO_ANSWERS},
  {name: 'pick examples', step: 'pick', answers: EXAMPLES},
  {name: 'pick example chosen', step: 'pick', answers: STRUCTURE},
  {name: 'pick projects', step: 'pick', answers: PROJECTS},
  {name: 'pick project task', step: 'pick', answers: {...OWN, task: null}},
  {name: 'review LLM', step: 'review', answers: LLM},
  {name: 'review LLM clusters', step: 'review', answers: LLM, machine: 'local'},
  {name: 'review data structure', step: 'review', answers: STRUCTURE},
  {name: 'review database', step: 'review', answers: DATABASE},
  {name: 'review project', step: 'review', answers: OWN},
  {name: 'review roles', step: 'review', answers: OWN, agent: JUDGE_ON_SONNET},
  {name: 'review signed out', step: 'review', answers: OWN, logins: {claude: 'signed_out'}},
];

/** `seed` with the machine saying what `at` says of its harness CLIs' logins. */
function withLogins(seed: FakeBridgeSeed, at: NewRunPage): FakeBridgeSeed {
  if (at.logins === undefined || seed.newRun === undefined) return seed;
  const machine = {...(seed.newRun.machine as object), harness_logins: at.logins};
  return {...seed, newRun: {...seed.newRun, machine}};
}

/** The draft that has `answers` made and `step` shown, on the machine the world's runs are on. */
function draftAt(page: NewRunPage, machine = 'ssh:host-b'): Draft {
  return {
    machine: page.machine ?? machine,
    answers: page.answers,
    at: page.step,
    agent: page.agent ?? null,
    budget: {rounds: null, timeLimitS: null, results: 'keep'},
    measure: {kind: 'same_machine'},
    advanced: '',
    destination: null,
  };
}

/** Opens the app showing `page`, as if its answers had been given in this window. */
export async function openAt(
  page: Page,
  at: NewRunPage,
  seed: FakeBridgeSeed = SEED,
): Promise<void> {
  const text = storeDraft(draftAt(at));
  await page.addInitScript(
    ([key, value]) => window.sessionStorage.setItem(key as string, value as string),
    [DRAFT_KEY, text],
  );
  await openNewRun(page, withLogins(seed, at));
  await expect(page.getByRole('navigation', {name: 'New run steps'})).toBeVisible();
  // The bundled fonts swap in after first paint (`font-display: swap`), and a width measured
  // before the swap differs by a pixel from one measured after it. The first layout has started
  // every font load the page needs, so waiting for the loads to finish makes sizes final.
  await page.evaluate(() => document.fonts.ready);
}

export const main = (page: Page) => page.getByRole('main');
export const steps = (page: Page) => page.getByRole('navigation', {name: 'New run steps'});
export const question = (page: Page, title: string | RegExp) =>
  main(page).getByRole('heading', {level: 1, name: title});

/** Choose `name` in the question's group `group`. */
export async function choose(page: Page, group: string, name: string | RegExp): Promise<void> {
  await main(page)
    .getByRole('radiogroup', {name: group, exact: true})
    .getByRole('radio', {name})
    .check();
}

/** Press Continue (or `label`) and wait for the page after it. */
export async function proceed(page: Page, label = 'Continue'): Promise<void> {
  await main(page).getByRole('button', {name: label, exact: true}).click();
}

/** The examples table's row of `example`'s `task` (`default` when not given). */
export function exampleRow(page: Page, example: string, task = 'default'): Locator {
  const rows = main(page)
    .getByRole('row')
    .filter({has: page.getByText(example, {exact: true})});
  return task === 'default' ? rows : rows.filter({has: page.getByText(task, {exact: true})});
}

/** On the pick page, pick `example`'s `task` and go on to Review. */
export async function pickExample(page: Page, example: string, task = 'default'): Promise<void> {
  await choose(page, 'Start from', 'Examples');
  await exampleRow(page, example, task).click();
  await proceed(page);
  await expect(question(page, 'Review and start')).toBeVisible();
}
