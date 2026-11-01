/**
 * The guided New run, journey by journey, drawn in a DOM over the world of the browser journeys
 * (`testing/shell-world.tsx`): the pages a person walks through, what each says, what Start run
 * sends the host. These held no layout, so they ran in Chromium for nothing, about 0.5 s a page
 * load; here a page is drawn by the shell's own stores and screens in about 25 ms. The browser
 * keeps one walk through each path (`e2e/new-run.spec.ts`) and the checks of where things sit.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import type {ExampleList} from '@vibesys/contracts/host-helper';
import {REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import {
  exampleCatalogue,
  exampleCatalogueWithoutDetails,
  queueRsCatalogue,
} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {fillIn, pickLabel, press} from './testing/dom-controls.js';
import {
  all,
  byText,
  checked,
  closed,
  drawnTexts,
  field,
  maybe,
  nameOfControl,
  notSee,
  one,
  pickValue,
  see,
  settle,
  textOf,
  until,
} from './testing/dom-query.js';
import type {FakeBridgeSeed} from './testing/fake-bridge.js';
import {
  asked,
  choose,
  main,
  proceed,
  question,
  stepButton,
  steps,
} from './testing/new-run-driver.js';
import {draftAt, NEW_RUN_PAGES, type NewRunPage} from './testing/new-run-pages.js';
import {rawTextIn} from './testing/raw-text.js';
import {RAW_TEXT_INPUT} from './testing/raw-text-input.js';
import {PROJECT, SEED} from './testing/seed.js';
import {type OpenShell, openShell} from './testing/shell-world.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const shells: OpenShell[] = [];
let clock = new FakeClock();
afterEach(async () => {
  for (const shell of shells.splice(0)) await shell.close();
});

function pageNamed(name: string): NewRunPage {
  const found = NEW_RUN_PAGES.find(each => each.name === name);
  if (found === undefined) throw new Error(`no New run page named ${name}`);
  return found;
}

const newRunOf = (seed: FakeBridgeSeed): NonNullable<FakeBridgeSeed['newRun']> => {
  if (seed.newRun === undefined) throw new Error('the seed has no New run helper');
  return seed.newRun;
};

/** A seed whose helper offers `change` on top of the world's. */
function withNewRun(change: Partial<NonNullable<FakeBridgeSeed['newRun']>>): FakeBridgeSeed {
  return {...SEED, newRun: {...newRunOf(SEED), ...change}};
}

/** The app on New run, as a person starts it: no answer given yet. */
async function open(seed: FakeBridgeSeed = SEED): Promise<OpenShell> {
  clock = new FakeClock();
  const shell = await openShell({kind: 'new-run'}, {schedule: clock.schedule, seed});
  shells.push(shell);
  return shell;
}

/** The app showing `at`, as if its answers had been given in this window. */
async function openAt(at: NewRunPage, seed: FakeBridgeSeed = SEED): Promise<OpenShell> {
  clock = new FakeClock();
  const shell = await openShell(
    {kind: 'new-run'},
    {draft: draftAt(at), schedule: clock.schedule, seed},
  );
  shells.push(shell);
  return shell;
}

const button = (scope: Element, name: string | RegExp, exact = false): HTMLElement =>
  one(scope, {role: 'button', name, exact});
const start = (shell: OpenShell): HTMLElement => button(main(shell), 'Start run', true);
const startClosed = (shell: OpenShell): boolean => closed(start(shell));
const radiogroup = (scope: Element, name: string): HTMLElement =>
  one(scope, {role: 'radiogroup', name, exact: true});
const radio = (scope: Element, name: string | RegExp, exact = false): HTMLElement =>
  one(scope, {role: 'radio', name, exact});
const asking = (shell: OpenShell, title: string | RegExp): boolean =>
  question(shell, title) !== null;

async function expectQuestion(shell: OpenShell, title: string | RegExp): Promise<void> {
  await until(`the question ${String(title)}`, () => asking(shell, title));
}

/** Lets the field's pause pass, and the listing it asked for arrive. */
async function pause(): Promise<void> {
  await act(async () => clock.advanceBy(1000));
  await settle();
}

async function pressStart(shell: OpenShell): Promise<void> {
  await press(start(shell));
  await settle();
}

/** The runs the host was asked to start. */
const started = () => asked().started;

test('an example data structure: its structure, its workload, a goal with no direction made up', async () => {
  const shell = await open();
  await choose(shell, 'Start from', 'An example');
  await proceed(shell);
  await choose(shell, 'Domain', 'Data structures');
  await proceed(shell);
  await expectQuestion(shell, 'Which structure?');
  await choose(shell, 'Structure', /stack/);
  await proceed(shell);
  await expectQuestion(shell, 'Which workload?');
  radio(main(shell), /Single producer, single consumer/);
  await choose(shell, 'Workload', /Multiple producers, multiple consumers/);
  await proceed(shell);
  await see(main(shell), 'Optimize a multi-producer, multi-consumer bounded stack.');
  // The task declares no objectives: no direction is shown for its metric.
  notSee(main(shell), /Higher is better|Lower is better/);
  await proceed(shell, 'Looks right');
  await proceed(shell);
  await proceed(shell);
  await press(button(main(shell), 'Create the project'));
  await settle();
  await until('Start run to open', () => !startClosed(shell));
  await pressStart(shell);
  expect(started()).toMatchObject([{project: '~/vibesys-projects/stack', task: 'mpmc'}]);
});

test('an example of the real catalogue shows its goal like a project of the user: the file, the text, what it optimizes', async () => {
  const shell = await open(withNewRun({examples: queueRsCatalogue}));
  await choose(shell, 'Start from', 'An example');
  await proceed(shell);
  await choose(shell, 'Domain', 'Data structures');
  await proceed(shell);
  await choose(shell, 'Structure', /queue-rs/);
  await proceed(shell);
  await choose(shell, 'Workload', /Multiple producers, multiple consumers/);
  await proceed(shell);
  await see(main(shell), '.vibesys/tasks/mpmc/OBJECTIVE.md');
  await see(main(shell), 'Optimize a multi-producer, multi-consumer bounded FIFO queue');
  await see(main(shell), 'Higher is better');
  await see(main(shell), 'total_ops_per_sec');
  notSee(main(shell), 'Goal not shown here');
});

test('a machine whose VibeSys cannot read a goal says Needs an update, not that the goal is hidden', async () => {
  const shell = await openAt(
    pageNamed('goal project'),
    withNewRun({features: [...REQUIRED_HELPER_FEATURES, 'run_roles', 'example_catalogue']}),
  );
  await see(main(shell), 'Needs an update');
  await see(main(shell), '.vibesys/tasks/mpmc/OBJECTIVE.md');
  notSee(main(shell), 'Goal not shown here');
  notSee(main(shell), 'Higher is better');
});

/** The example catalogue with `extra` models, and Qwen3.5 9B also served on an NVIDIA H100. */
function longCatalogue(extra: number): ExampleList {
  const base = exampleCatalogue.examples;
  const qwen = base.find(each => each.name === 'model-serving/qwen3.5-9b-mi210');
  if (qwen === undefined) throw new Error('the fixture has no Qwen example');
  const served = (name: string, model: string, accelerator: string) => ({
    ...qwen,
    name,
    task_details: (qwen.task_details ?? []).map(task => ({
      ...task,
      model: {name: model, maker: 'other' as const},
      hardware: {accelerator, count: 1, vendor: 'nvidia' as const, backend: 'cuda' as const},
    })),
  });
  const twin = {
    ...served('model-serving/qwen3.5-9b-h100', 'Qwen3.5 9B', 'NVIDIA H100'),
    task_details: (qwen.task_details ?? []).map(task => ({
      ...task,
      hardware: {
        accelerator: 'NVIDIA H100',
        count: 1,
        vendor: 'nvidia' as const,
        backend: 'cuda' as const,
      },
    })),
  };
  const many = Array.from({length: extra}, (_, index) =>
    served(`model-serving/long-${index}`, `Model ${index}`, 'NVIDIA L4'),
  );
  return {...exampleCatalogue, examples: [...base, twin, ...many]};
}

test('a model is one card however many kinds of hardware it has examples for, and hardware follows', async () => {
  const shell = await openAt(pageNamed('model'), withNewRun({examples: longCatalogue(0)}));
  const qwen = all(radiogroup(main(shell), 'Qwen'), {role: 'radio'});
  expect(qwen.map(each => nameOfControl(each))).toEqual(['Qwen3.5 9B']);
  await choose(shell, 'Qwen', /Qwen3\.5 9B/);
  await proceed(shell);
  expect(all(main(shell), {role: 'radio'})).toHaveLength(2);
  expect(textOf(radiogroup(main(shell), 'AMD'))).toContain('MI210');
  expect(textOf(radiogroup(main(shell), 'NVIDIA'))).toContain('H100');
});

test('a step draws no explanatory sentence under its question, and a blocked Continue no caption', async () => {
  const shell = await openAt(pageNamed('start'));
  for (const sentence of [
    'Start from one of our examples',
    'A ready-made task',
    'Your own repository',
    'Answer the question above',
  ]) {
    notSee(main(shell), sentence);
  }
  expect(button(main(shell), 'Continue').getAttribute('aria-disabled')).toBe('true');
});

test('the steps show their answers and go back to them; a later question waits for the earlier', async () => {
  const shell = await openAt(pageNamed('goal LLM'));
  const item = (label: string): HTMLElement => stepButton(shell, label);
  expect(textOf(item('Start from'))).toContain('An example');
  expect(textOf(item('Domain'))).toContain('LLM serving');
  expect(textOf(item('Model'))).toContain('Qwen3.5 9B');
  expect(textOf(item('Hardware'))).toContain('1× AMD MI210');
  // The goal is open; the steps after it cannot be opened before it is read.
  expect(closed(item('Where it runs'))).toBe(true);
  expect(closed(item('Review and start'))).toBe(true);
  await press(item('Model'));
  await settle();
  await expectQuestion(shell, 'Which model do you want to serve?');
  expect(item('Model').getAttribute('aria-current')).toBe('page');
  // Choosing another model clears the hardware that followed it.
  await choose(shell, 'Meta', /Llama 3 8B/);
  expect(textOf(item('Hardware'))).not.toContain('MI210');
  // The goal cannot be reached before the hardware it depends on is chosen.
  expect(closed(item('Goal'))).toBe(true);
  await proceed(shell);
  expect(button(main(shell), 'Continue').getAttribute('aria-disabled')).toBe('true');
});

test('every step has its number in a neutral circle, the current one filled, no status colour', async () => {
  const shell = await openAt(pageNamed('goal LLM'));
  const markers = [...shell.container.querySelectorAll('.vs-sidebar-marker')];
  expect(markers.map(each => each.textContent)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8']);
  expect(markers.map(each => each.getAttribute('data-state'))).toEqual([
    'neutral',
    'neutral',
    'neutral',
    'neutral',
    'current',
    'neutral',
    'neutral',
    'muted',
  ]);
  // No marker carries a status dot.
  expect(
    markers.map(each => each.closest('.vs-sidebar-item')?.querySelectorAll('.vs-dot').length),
  ).toEqual(markers.map(() => 0));
  // The collapsed control (shown when narrow) counts the same steps.
  await see(shell.container, '5 of 8 · Goal');
});

test('changing the domain clears the questions of the old one, and Back returns a page', async () => {
  const shell = await openAt(pageNamed('goal data structure'));
  await press(one(steps(shell), {role: 'button', name: /Domain/}));
  await settle();
  await choose(shell, 'Domain', 'Databases');
  await proceed(shell);
  await expectQuestion(shell, 'Which example?');
  expect(all(steps(shell), {role: 'button', name: /Structure/})).toHaveLength(0);
  await press(button(main(shell), 'Back'));
  await settle();
  await expectQuestion(shell, 'Which domain?');
});

test('Start over forgets the answers', async () => {
  const shell = await openAt(pageNamed('goal LLM'));
  await press(one(steps(shell), {role: 'button', name: 'Start over'}));
  await settle();
  await expectQuestion(shell, 'What do you want to optimize?');
  expect(all(steps(shell), {role: 'button', name: 'Start over'})).toHaveLength(0);
});

/** The rail: the app's one navigation. */
const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});

async function showScreen(shell: OpenShell, destination: string): Promise<void> {
  await press(one(rail(shell), {role: 'button', name: destination, exact: destination === 'New'}));
  await shell.read();
}

test('an unfinished run is a draft: it is offered on the Runs dashboard, kept across a reload, and discarded', async () => {
  const seed = SEED;
  const home = async (shell: OpenShell): Promise<void> => {
    await showScreen(shell, 'Runs');
    one(main(shell), {role: 'heading', name: 'Runs'});
  };
  let shell = await open(seed);
  await home(shell);
  notSee(main(shell), 'Draft', true);
  await showScreen(shell, 'New');
  await choose(shell, 'Start from', 'An example');
  await proceed(shell);
  await choose(shell, 'Domain', 'LLM serving');
  await proceed(shell);
  // Leaving New run and coming back finds it where it was left.
  await home(shell);
  await see(main(shell), 'LLM serving');
  await see(main(shell), '3 of 8 · Model');
  expect(asking(shell, 'Which model do you want to serve?')).toBe(false);
  await press(button(main(shell), 'Open'));
  await settle();
  await expectQuestion(shell, 'Which model do you want to serve?');
  // A reload: the window's storage stays, the app starts again.
  await shell.close();
  shells.pop();
  shell = await openShell(
    {kind: 'runs'},
    {schedule: new FakeClock().schedule, seed, keepStorage: true},
  );
  shells.push(shell);
  await press(button(main(shell), 'Open'));
  await settle();
  await expectQuestion(shell, 'Which model do you want to serve?');
  expect(textOf(stepButton(shell, 'Domain'))).toContain('LLM serving');
  await home(shell);
  await press(button(main(shell), 'Discard'));
  await settle();
  notSee(main(shell), 'Draft', true);
  await showScreen(shell, 'New');
  await expectQuestion(shell, 'What do you want to optimize?');
});

test('a helper without the catalogue lists examples and their tasks, with no domain or goal text', async () => {
  const shell = await open(
    withNewRun({
      examples: exampleCatalogueWithoutDetails,
      features: [...REQUIRED_HELPER_FEATURES, 'run_roles'],
    }),
  );
  await choose(shell, 'Start from', 'An example');
  await proceed(shell);
  await expectQuestion(shell, 'Which example?');
  await choose(shell, 'Example', /olmo-hybrid-prefix-caching/);
  await proceed(shell);
  await expectQuestion(shell, 'Which task?');
  await choose(shell, 'Task', 'vllm');
  await proceed(shell);
  await see(main(shell), 'Needs an update');
  await see(main(shell), '.vibesys/tasks/vllm/OBJECTIVE.md');
  notSee(main(shell), 'Goal not shown here');
});

test('a machine with no examples dims that start, and says so', async () => {
  const shell = await open(withNewRun({examples: {...newRunOf(SEED).examples, examples: []}}));
  expect(closed(radio(main(shell), /An example/))).toBe(true);
  await see(main(shell), 'No examples');
});

test('a machine whose VibeSys predates New run says so and is never asked to start', async () => {
  const shell = await openAt(pageNamed('review project'), withNewRun({features: ['harnesses']}));
  await see(main(shell), 'VibeSys on host-b');
  await see(main(shell), 'Needs an update');
  expect(startClosed(shell)).toBe(true);
  await pressStart(shell);
  expect(started()).toEqual([]);
  expect(asked().preflights).toEqual([]);
});

const withoutRunTrace = (): FakeBridgeSeed =>
  withNewRun({features: REQUIRED_HELPER_FEATURES.filter(feature => feature !== 'run_trace')});

test('a machine that serves everything but the run trace says Needs an update on Where', async () => {
  const shell = await openAt(pageNamed('where'), withoutRunTrace());
  await see(main(shell), 'Needs an update', true);
  expect(
    main(shell).querySelector('[aria-description="VibeSys on host-b needs an update"]'),
  ).not.toBeNull();
});

test('a machine that serves everything but the run trace says Needs an update on Review and never starts', async () => {
  const shell = await openAt(pageNamed('review project'), withoutRunTrace());
  await see(main(shell), 'VibeSys on host-b');
  await see(main(shell), 'Needs an update');
  expect(startClosed(shell)).toBe(true);
  await pressStart(shell);
  expect(started()).toEqual([]);
});

test('a start whose reply was lost says on the page that it may have started, and Start waits until Runs was opened', async () => {
  const shell = await openAt(pageNamed('review project'));
  // The host takes the run and the reply is lost: the call rejects with nothing reported.
  const bridge = (globalThis.window as unknown as {vibesysWelcome: Record<string, unknown>})
    .vibesysWelcome;
  bridge['startRun'] = () => Promise.reject(new Error('the reply was lost'));
  await pressStart(shell);
  // The words are on the page, Start is closed with its reason beside it, and there is a way to look.
  await see(main(shell), 'May have started', true);
  expect(startClosed(shell)).toBe(true);
  await see(main(shell), 'Check Runs first', true);
  await pressStart(shell);
  expect(started()).toEqual([]);

  await press(button(main(shell), 'Open Runs', true));
  await shell.read();
  expect(one(rail(shell), {role: 'button', name: 'Runs'}).getAttribute('aria-current')).toBe(
    'page',
  );
  // Back at the form: the person has looked, so Start is open again.
  await showScreen(shell, 'New');
  notSee(main(shell), 'May have started', true);
  expect(startClosed(shell)).toBe(false);
});

test('a project inside another repository is copied out by its fix, then may start', async () => {
  const shell = await open();
  await choose(shell, 'Start from', 'Your project');
  await proceed(shell);
  await fillIn(
    field(main(shell), 'Folder on host-b') as HTMLInputElement,
    '/home/me/vibesys/examples/queue-rs',
  );
  await settle();
  await press(button(main(shell), 'Use this folder'));
  await settle();
  await proceed(shell);
  await choose(shell, 'Task', 'mpmc');
  await proceed(shell);
  await proceed(shell, 'Looks right');
  await proceed(shell);
  await proceed(shell);
  await see(main(shell), 'Copy to its own repository');
  await see(main(shell), 'Its copy goes to /home/me/vibesys-projects/queue-rs-2.');
  expect(startClosed(shell)).toBe(true);
  await see(main(shell), 'Fix the items marked above to start.');
  await press(button(main(shell), 'Copy to its own repository'));
  await settle();
  await until(
    'the copy button to go',
    () => maybe(main(shell), {role: 'button', name: 'Copy to its own repository'}) === null,
  );
  await until('Start run to open', () => !startClosed(shell));
  await pressStart(shell);
  expect(started()).toMatchObject([
    {project: '/home/me/vibesys-projects/queue-rs-2', task: 'mpmc'},
  ]);
});

test('the folder field completes paths on the machine', async () => {
  const shell = await open();
  await choose(shell, 'Start from', 'Your project');
  await proceed(shell);
  const folder = () =>
    one(main(shell), {role: 'combobox', name: 'Folder on host-b'}) as HTMLInputElement;
  await fillIn(folder(), '/home/me/pro');
  await pause();
  const option = (name: string) => one(shell.container, {role: 'option', name});
  await press(option('/home/me/projects/'));
  await pause();
  expect(folder().value).toBe('/home/me/projects/');
  await press(option('/home/me/projects/queue-rs/'));
  await pause();
  expect(folder().value).toBe('/home/me/projects/queue-rs/');
  await press(button(main(shell), 'Use this folder'));
  await settle();
  await proceed(shell);
  await expectQuestion(shell, 'Which task?');
  radio(main(shell), 'mpmc', true);
});

const moreOptions = (shell: OpenShell): HTMLElement => button(main(shell), /More options/);

test('extra arguments may not repeat an option set above, and roles change the run', async () => {
  const shell = await openAt(pageNamed('review project'));
  expect(startClosed(shell)).toBe(false);
  await press(button(main(shell), 'More options'));
  await settle();
  const extra = () => field(main(shell), 'Extra arguments') as HTMLInputElement;
  await fillIn(extra(), '--max-rounds 3');
  await settle();
  expect(textOf(main(shell)).split('--max-rounds is set above').length - 1).toBe(2);
  expect(startClosed(shell)).toBe(true);
  await fillIn(extra(), '--max-in-flight 3');
  await settle();
  await pickValue(field(main(shell), 'Time limit') as HTMLSelectElement, '3600');
  await press(button(main(shell), /^Reasoning effort:/));
  await settle();
  await pickValue(field(main(shell), 'Reviewer reasoning effort') as HTMLSelectElement, 'high');
  expect(startClosed(shell)).toBe(false);
  await pressStart(shell);
  expect(started()).toMatchObject([
    {
      project: PROJECT,
      task: 'mpmc',
      options: {
        time_limit_s: 3600,
        roles: [{role: 'multi-judge', model: null, reasoning_effort: 'high'}],
      },
      args: ['--max-in-flight', '3'],
    },
  ]);
});

test('a failing check lists itself first, and the error stays when More options is closed', async () => {
  const shell = await openAt(pageNamed('review project'));
  await press(button(main(shell), 'More options'));
  await settle();
  await fillIn(field(main(shell), 'Extra arguments') as HTMLInputElement, '--max-rounds 3');
  await settle();
  await press(button(main(shell), 'More options'));
  await settle();
  expect(all(main(shell), {role: 'textbox', name: 'Extra arguments'})).toHaveLength(0);
  expect(textOf(moreOptions(shell))).toContain('--max-rounds is set above');
  await see(main(shell), '1 check needs attention');
  expect(startClosed(shell)).toBe(true);
});

test('a machine whose VibeSys does not report its roles says every role runs as the run', async () => {
  const shell = await openAt(
    pageNamed('review project'),
    withNewRun({features: REQUIRED_HELPER_FEATURES}),
  );
  await press(button(main(shell), 'More options'));
  await settle();
  await see(shell.container, /does not report its agent roles/);
  expect(all(main(shell), {role: 'button', name: /^Reasoning effort:/})).toHaveLength(0);
});

test('an agent for a role: closed until opened, chosen from the run harness, listed on Review, sent as agents', async () => {
  const shell = await openAt(pageNamed('agent project'));
  const toggle = button(main(shell), /Use a different agent for a role/);
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  expect(textOf(toggle)).toContain('Same as the run');
  await press(toggle);
  await settle();
  const judge = () => field(main(shell), 'Reviewer model') as HTMLSelectElement;
  expect(judge().value).toBe('');
  // Models sit under their maker and family; the run's own harness is the only one a role may use.
  const groups = [...judge().querySelectorAll('optgroup')];
  expect(groups.map(each => each.getAttribute('label'))).toEqual(['Anthropic · Claude']);
  expect(all(main(shell), {role: 'combobox', name: 'Reviewer harness'})).toHaveLength(0);
  await pickLabel(judge(), 'Sonnet 4.5');
  expect(judge().value).not.toBe('');
  // The run's own model is the same as no choice: the role follows the run again.
  await pickLabel(judge(), 'Opus 4.5');
  expect(judge().value).toBe('');
  await pickLabel(judge(), 'Sonnet 4.5');
  await proceed(shell);
  await see(main(shell), 'Reviewer agent');
  await see(main(shell), 'Sonnet 4.5 · Claude Code');
  const row = byText(main(shell), 'Reviewer agent', true)[0];
  let scope: Element | null = row?.parentElement ?? null;
  while (scope !== null && scope.querySelector('button') === null) scope = scope.parentElement;
  if (scope === null) throw new Error('the Reviewer agent row has no button');
  await press(one(scope, {role: 'button', name: 'Change'}));
  await settle();
  expect(judge()).toBeDefined();
  await proceed(shell);
  expect(startClosed(shell)).toBe(false);
  await pressStart(shell);
  expect(started()).toMatchObject([
    {
      agents: {'multi-judge': {provider: 'claude', model: 'claude-sonnet-4-5'}},
      options: {roles: []},
    },
  ]);
});

test('a machine whose VibeSys cannot run an agent per role says Needs an update on that row, never beside an installed harness', async () => {
  const shell = await openAt(
    pageNamed('agent project'),
    withNewRun({features: [...REQUIRED_HELPER_FEATURES, 'run_roles']}),
  );
  // One status for the row that cannot be used; the harness card keeps saying it is installed.
  const label = byText(main(shell), 'Use a different agent for a role', true)[0];
  expect(textOf(label?.parentElement as Element)).toContain('Needs an update');
  expect(textOf(radiogroup(main(shell), 'Harness'))).toContain('Installed on host-b');
  notSee(main(shell), 'Not supported on this machine');
  expect(all(main(shell), {role: 'button', name: /different agent for a role/})).toHaveLength(0);
});

test('Where: a machine with nothing set up links to its Settings, and Back returns to the same step', async () => {
  const shell = await openAt(pageNamed('where'));
  const measured = radiogroup(main(shell), 'Measured on');
  expect(all(measured, {role: 'radio'})).toHaveLength(1);
  radio(measured, /Same machine/);
  await press(button(main(shell), 'Set up on host-b'));
  await shell.read();
  expect(
    (one(shell.container, {role: 'combobox', name: 'Machine'}) as HTMLSelectElement).value,
  ).toBe('ssh:host-b');
  expect(all(main(shell), {role: 'heading', name: 'Clusters and clouds'}).length).toBeGreaterThan(
    0,
  );
  await press(button(shell.container, 'Back', true));
  await shell.read();
  await expectQuestion(shell, /Where does the agent work/);
  expect(stepButton(shell, 'Where it runs').getAttribute('aria-current')).toBe('page');
  expect(checked(radio(radiogroup(main(shell), 'Agent works on'), /host-b/))).toBe(true);
});

test('Where: only what the machine has set up is offered, and the choice is the measurement target', async () => {
  const shell = await openAt(pageNamed('where'));
  await press(radio(radiogroup(main(shell), 'Agent works on'), /This Mac/));
  await settle();
  const measured = () => radiogroup(main(shell), 'Measured on');
  expect(all(measured(), {role: 'radio'})).toHaveLength(5);
  for (const name of [
    'Same machine',
    'Slurm · lab-h100',
    'Slurm · campus',
    'Slurm · cluster-m',
    'Modal',
  ]) {
    radio(measured(), new RegExp(name));
  }
  expect(all(main(shell), {role: 'button', name: /^Set up on/})).toHaveLength(0);
  await press(radio(measured(), 'Slurm · lab-h100', true));
  await settle();
  await proceed(shell);
  await proceed(shell);
  await see(main(shell), 'Slurm · lab-h100');
  await press(button(main(shell), 'Create the project'));
  await settle();
  await until('Start run to open', () => !startClosed(shell));
  await pressStart(shell);
  expect(started()).toMatchObject([{measure_on: {kind: 'slurm', cluster: 'lab-h100'}}]);
});

test('Where: a GPU task the agent machine lacks is a misfit only when results are measured there', async () => {
  const where = pageNamed('where');
  const shell = await openAt({
    ...where,
    answers: {
      ...where.answers,
      model: 'Olmo Hybrid 7B',
      hardware: 'nvidia|NVIDIA L4|1',
      variant: 'from-scratch',
    },
    machine: 'local',
  });
  const measured = () => radiogroup(main(shell), 'Measured on');
  const word = (text: string): Element[] => byText(main(shell), text, true);
  expect(word('Hardware mismatch')).toHaveLength(1);
  // The detail is the status word's description, not on the page.
  notSee(main(shell), /The task targets/);
  expect(word('Hardware mismatch')[0]?.getAttribute('aria-description')).toMatch(
    /^The task targets .*, but .* has /,
  );
  for (const name of ['Slurm · lab-h100', 'Slurm · campus', 'Modal']) {
    await press(radio(measured(), new RegExp(name)));
    await settle();
    expect(word('Hardware mismatch')).toHaveLength(0);
    expect(word('Hardware fits')).toHaveLength(0);
  }
  await press(radio(measured(), /Same machine/));
  await settle();
  expect(word('Hardware mismatch')).toHaveLength(1);
});

test('More options starts closed, says what it holds, and stays as left on each machine', async () => {
  let shell = await openAt(pageNamed('review project'));
  expect(moreOptions(shell).getAttribute('aria-expanded')).toBe('false');
  expect(textOf(moreOptions(shell))).toContain('Defaults');
  await press(moreOptions(shell));
  await settle();
  field(main(shell), 'Extra arguments');
  // A reload: the window's storage stays, the app starts again.
  await shell.close();
  shells.pop();
  shell = await openShell(
    {kind: 'new-run'},
    {schedule: new FakeClock().schedule, seed: SEED, keepStorage: true},
  );
  shells.push(shell);
  expect(moreOptions(shell).getAttribute('aria-expanded')).toBe('true');
});

test('the harness is chosen from tiles with a mark and a name each', async () => {
  const shell = await openAt(pageNamed('agent'));
  const harness = () => radiogroup(main(shell), 'Harness');
  expect(all(harness(), {role: 'radio'})).toHaveLength(1);
  expect(harness().querySelectorAll('svg.vs-brand-mark')).toHaveLength(1);
  expect(textOf(harness())).toContain('Installed on host-b');
  expect(all(radiogroup(main(shell), 'Model line'), {role: 'radio'})).toHaveLength(2);
  await choose(shell, 'Model line', 'OpenAI');
  const model = field(main(shell), 'Model', true) as HTMLSelectElement;
  expect(model.value).toBe('gpt-5-codex');
  expect(
    [...model.options].filter(option => option.selected).map(option => textOf(option)),
  ).toEqual(['GPT-5 Codex']);
  expect(textOf(harness())).toContain('Codex');
});

test("Machines' New run here opens New run on that machine", async () => {
  const shell = await openAt(pageNamed('where'));
  expect(checked(radio(radiogroup(main(shell), 'Agent works on'), /host-b/))).toBe(true);
  await showScreen(shell, 'Settings');
  await press(
    one(one(shell.container, {role: 'navigation', name: 'Settings sections'}), {
      role: 'button',
      name: /Machines/,
    }),
  );
  await shell.read();
  const tree = one(shell.container, {role: 'tree', name: 'Machines'});
  await press(one(tree, {role: 'treeitem', name: 'This Mac'}));
  await settle();
  await press(button(shell.container, 'New run here'));
  await shell.read();
  await expectQuestion(shell, /Where does the agent work/);
  expect(checked(radio(radiogroup(main(shell), 'Agent works on'), /This Mac/))).toBe(true);
});

test('a bridge call that throws is worded plainly, names what failed, and Try again reloads it', async () => {
  let real: unknown;
  const shell = await openShell(
    {kind: 'new-run'},
    {
      schedule: new FakeClock().schedule,
      prepare: bridge => {
        // The agents' request is missing, as a bridge built before it would be.
        real = bridge['harnesses'];
        delete bridge['harnesses'];
      },
    },
  );
  shells.push(shell);
  const failed = () => byText(shell.container, 'Not read', true)[0];
  await until('Not read', () => failed() !== undefined);
  const description = failed()?.getAttribute('aria-description') ?? '';
  expect(description).toMatch(/^Could not read the agents host-b runs\./);
  expect(description).toMatch(/Something unexpected happened\./);
  expect(description).not.toMatch(/is not a function/);
  expect(textOf(shell.container)).not.toMatch(/is not a function/);

  (globalThis.window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome[
    'harnesses'
  ] = real;
  await press(button(shell.container, 'Try again'));
  await settle();
  expect(all(shell.container, {role: 'button', name: 'Try again'})).toHaveLength(0);
});

test('models by maker: the latest generation is shown and older ones wait behind Older (n)', async () => {
  const offer = (model: string, tier: number, tierName: string, generation: number) => ({
    provider: 'codex',
    model,
    default: model === 'gpt-6-sol',
    display: {
      providerName: 'Codex',
      modelName: model.toUpperCase(),
      family: 'GPT',
      tier,
      tierName,
      maker: 'openai',
      generation: {id: `g${generation}`, name: `GPT-${7 - generation}`, order: generation},
    },
  });
  const shell = await openAt(
    pageNamed('agent'),
    withNewRun({
      harnesses: [
        offer('gpt-6-luna', 1, 'Luna', 1),
        offer('gpt-6-sol', 2, 'Sol', 1),
        offer('gpt-5-luna', 1, 'Luna', 2),
        offer('gpt-5-sol', 2, 'Sol', 2),
      ],
    }),
  );
  expect(checked(radio(main(shell), 'OpenAI', true))).toBe(true);
  const generation = () => radiogroup(main(shell), 'Generation');
  expect(all(generation(), {role: 'radio'})).toHaveLength(1);
  expect(textOf(generation())).toContain('GPT-6 (latest)');
  await press(button(main(shell), 'Older (1)'));
  await settle();
  expect(all(generation(), {role: 'radio'})).toHaveLength(2);
  await press(radio(generation(), 'GPT-5', true));
  await settle();
  // The tier the reader was on is kept in the older generation.
  expect(one(main(shell), {role: 'slider', name: 'Model'}).getAttribute('aria-valuetext')).toBe(
    'Sol',
  );
  await see(main(shell), 'GPT-5-SOL', true);
  expect(all(main(shell), {role: 'button', name: /Older/})).toHaveLength(0);
});

test('a machine that cannot list its examples says Failed and lets another machine be chosen', async () => {
  const shell = await openAt(pageNamed('example'), withNewRun({examplesFail: ['ssh:host-b']}));
  await see(main(shell), 'Failed', true);
  notSee(main(shell), 'Listing', true);
  // The failed page is replaced by the new machine's listing as the choice lands.
  await press(radio(radiogroup(main(shell), 'Machine'), /This Mac/));
  await settle();
  await expectQuestion(shell, 'Which domain?');
  radiogroup(main(shell), 'Domain');
  notSee(main(shell), 'Failed', true);
});

test('a fresh profile whose This Mac cannot list examples offers the other machines at once', async () => {
  const shell = await open({
    ...withNewRun({examplesFail: ['local']}),
    recent: [],
    listings: [],
  });
  await expectQuestion(shell, 'What do you want to optimize?');
  const from = () => radiogroup(main(shell), 'Start from');
  expect(textOf(from())).toContain('Failed');
  // The first page itself offers the configured machines, the broken one chosen.
  const machines = () => radiogroup(main(shell), 'Machine');
  expect(all(machines(), {role: 'radio'})).toHaveLength(2);
  expect(checked(radio(machines(), /This Mac/))).toBe(true);
  await press(radio(machines(), /host-b/));
  await settle();
  // Its examples are listed, so the example path opens.
  expect(textOf(from())).not.toContain('Failed');
  expect(textOf(from())).toContain('examples');
  expect(all(main(shell), {role: 'radiogroup', name: 'Machine', exact: true})).toHaveLength(0);
});

test('no page draws a role, harness, model or example id as raw text', async () => {
  const raw: string[] = [];
  for (const each of NEW_RUN_PAGES) {
    const shell = await openAt(each);
    one(main(shell), {role: 'heading', level: 1});
    raw.push(
      ...rawTextIn(drawnTexts(main(shell)), RAW_TEXT_INPUT).map(
        found => `${each.name}: ${found.message} (${found.where})`,
      ),
    );
    await shell.close();
    shells.pop();
  }
  expect(raw).toEqual([]);
});
