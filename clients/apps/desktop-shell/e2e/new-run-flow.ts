/**
 * Driving the guided New run: choosing an answer, going on, and opening a page directly. A page is
 * opened by giving the window the draft that would have brought it there (the draft is the
 * window's own storage, which the page reads as it starts), so a check of every page does not
 * click through the pages before it.
 */
import {expect, type Page} from '@playwright/test';
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
}

const EXAMPLE = {...NO_ANSWERS, from: 'example' as const};
const LLM = {
  ...EXAMPLE,
  domain: 'llm-serving' as const,
  model: 'Qwen3.5 9B',
  hardware: 'amd|AMD MI210|1',
};
const OLMO = {
  ...EXAMPLE,
  domain: 'llm-serving' as const,
  model: 'Olmo Hybrid 7B',
  hardware: 'nvidia|NVIDIA L4|1',
};
const STRUCTURE = {
  ...EXAMPLE,
  domain: 'data-structures' as const,
  structure: 'data-structures/stack',
  workload: 'mpmc',
};
const DATABASE = {...EXAMPLE, domain: 'databases' as const, example: 'database/kv-store'};
const OWN = {
  ...NO_ANSWERS,
  from: 'project' as const,
  project: {source: 'recent' as const, path: '/home/me/projects/queue-rs'},
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
  {name: 'start', step: 'start', answers: NO_ANSWERS},
  {name: 'domain', step: 'domain', answers: EXAMPLE},
  {name: 'model', step: 'model', answers: {...EXAMPLE, domain: 'llm-serving'}},
  {name: 'hardware', step: 'hardware', answers: {...LLM, hardware: null}},
  {name: 'variant', step: 'variant', answers: OLMO},
  {name: 'structure', step: 'structure', answers: {...EXAMPLE, domain: 'data-structures'}},
  {name: 'workload', step: 'workload', answers: {...STRUCTURE, workload: null}},
  {name: 'example', step: 'example', answers: {...EXAMPLE, domain: 'databases'}},
  {name: 'goal LLM', step: 'goal', answers: LLM},
  {name: 'goal data structure', step: 'goal', answers: STRUCTURE},
  {name: 'goal database', step: 'goal', answers: DATABASE},
  {name: 'where', step: 'where', answers: {...LLM, goalSeen: true}},
  {name: 'where clusters', step: 'where', answers: {...LLM, goalSeen: true}, machine: 'local'},
  {name: 'agent', step: 'agent', answers: {...LLM, goalSeen: true}},
  {name: 'agent project', step: 'agent', answers: {...OWN, goalSeen: true}},
  {
    name: 'agent roles',
    step: 'agent',
    answers: {...OWN, goalSeen: true},
    agent: JUDGE_ON_SONNET,
  },
  {name: 'review', step: 'review', answers: {...LLM, goalSeen: true}},
  {
    name: 'review roles',
    step: 'review',
    answers: {...OWN, goalSeen: true},
    agent: JUDGE_ON_SONNET,
  },
  {name: 'project', step: 'project', answers: {...NO_ANSWERS, from: 'project'}},
  {name: 'task', step: 'task', answers: {...OWN, task: null}},
  {name: 'goal project', step: 'goal', answers: OWN},
  {name: 'review project', step: 'review', answers: {...OWN, goalSeen: true}},
];

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
  await openNewRun(page, seed);
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
