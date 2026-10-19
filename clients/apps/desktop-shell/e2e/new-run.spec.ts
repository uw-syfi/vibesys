import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import {exampleCatalogueWithoutDetails} from '@vibesys/run-fixtures';
import {rawTextOf} from './checks/screen-check.js';
import {installFakeBridge, installFakeMachines, installFakeNewRun} from './fake-bridge.js';
import {installFakeRun} from './fake-run.js';
import {choose, main, NEW_RUN_PAGES, openAt, proceed, question, steps} from './new-run-flow.js';
import {type FakeHostWindow, open, PROJECT, SEED} from './world.js';

const start = (page: Page) => main(page).getByRole('button', {name: 'Start run'});

async function fakeNewRun(page: Page) {
  return page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.newRun);
}

function pageNamed(name: string) {
  const found = NEW_RUN_PAGES.find(each => each.name === name);
  if (found === undefined) throw new Error(`no New run page named ${name}`);
  return found;
}

test('an example of LLM serving: its model, its hardware, its goal, then the run starts', async ({
  page,
}) => {
  await open(page);
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await expect(main(page).getByText('New run · step 1 of 5')).toBeVisible();
  await choose(page, 'Start from', 'An example');
  await proceed(page);

  await expect(question(page, 'Which domain?')).toBeVisible();
  await choose(page, 'Domain', 'LLM serving');
  await proceed(page);

  // Models are grouped by their maker.
  await expect(question(page, 'Which model do you want to serve?')).toBeVisible();
  await expect(main(page).getByRole('radiogroup', {name: 'Meta'})).toBeVisible();
  await expect(main(page).getByRole('radiogroup', {name: 'Qwen'})).toBeVisible();
  await choose(page, 'Qwen', /Qwen3\.5 9B/);
  await proceed(page);

  // Hardware is grouped by vendor; one with no example for this model is dimmed and says so.
  await expect(question(page, 'Which hardware are you optimizing for?')).toBeVisible();
  await expect(main(page).getByRole('radiogroup', {name: 'NVIDIA'})).toBeVisible();
  const h100 = main(page).getByRole('radiogroup', {name: 'NVIDIA'}).getByRole('radio').first();
  await expect(h100).toBeDisabled();
  await expect(main(page).getByRole('radiogroup', {name: 'NVIDIA'})).toContainText(
    'No example yet',
  );
  await choose(page, 'AMD', /MI210/);
  await proceed(page);

  // The goal is the task's OBJECTIVE.md as written, with what it optimizes and its benchmark beside it.
  await expect(question(page, 'This is what the agent will work toward')).toBeVisible();
  await expect(
    main(page).getByText('Objective: Qwen3.5-9B agentic-coding-session serving'),
  ).toBeVisible();
  await expect(main(page).getByText('Higher is better')).toBeVisible();
  await expect(main(page).getByText('output_token_throughput_per_s')).toBeVisible();
  await expect(main(page).getByText('Lower is better')).toBeVisible();
  await expect(main(page).getByText('p90_latency_ms')).toBeVisible();
  await expect(main(page).getByText('Qwen3.5 9B (Qwen)')).toBeVisible();
  await proceed(page, 'Looks right');

  await expect(question(page, /Where does the agent work/)).toBeVisible();
  await expect(
    main(page)
      .getByRole('radiogroup', {name: 'Agent works on'})
      .getByRole('radio', {name: /host-b/}),
  ).toBeChecked();
  await expect(main(page).getByText('The hardware fits.')).toBeVisible();
  await proceed(page);

  await expect(question(page, 'Which agent does the work?')).toBeVisible();
  await expect(main(page).getByRole('radio', {name: 'Anthropic', exact: true})).toBeChecked();
  await expect(main(page).getByRole('slider', {name: 'Model'})).toHaveAttribute(
    'aria-valuetext',
    'Opus',
  );
  await main(page).getByRole('slider', {name: 'Model'}).press('ArrowLeft');
  await expect(main(page).getByText('Sonnet 4.5', {exact: true})).toBeVisible();
  await main(page).getByLabel('Ideas to try').selectOption('3');
  await proceed(page);

  // Review: every answer, and the example is not a project until it is made.
  await expect(question(page, 'Ready to start?')).toBeVisible();
  await expect(main(page)).toContainText('qwen3.5-9b-mi210 · default');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(main(page)).toContainText(/Ready · \d+ checks? passed/);
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toEqual([
    {
      project: '~/vibesys-projects/qwen3.5-9b-mi210',
      task: 'default',
      harness: {provider: 'claude', model: 'claude-sonnet-4-5'},
      options: {rounds: 3, results: 'keep', roles: []},
      agents: {},
      args: [],
    },
  ]);
});

test('an example data structure: its structure, its workload, a goal with no direction made up', async ({
  page,
}) => {
  await open(page);
  await choose(page, 'Start from', 'An example');
  await proceed(page);
  await choose(page, 'Domain', 'Data structures');
  await proceed(page);
  await expect(question(page, 'Which structure?')).toBeVisible();
  await choose(page, 'Structure', /stack/);
  await proceed(page);
  await expect(question(page, 'Which workload?')).toBeVisible();
  await expect(
    main(page).getByRole('radio', {name: /Single producer, single consumer/}),
  ).toBeVisible();
  await choose(page, 'Workload', /Multiple producers, multiple consumers/);
  await proceed(page);
  await expect(
    main(page).getByText('Optimize a multi-producer, multi-consumer bounded stack.'),
  ).toBeVisible();
  // The task declares no objectives: no direction is shown for its metric.
  await expect(main(page)).toContainText(
    'This task declares no objectives, so no direction is shown.',
  );
  await expect(main(page).getByText(/Higher is better|Lower is better/)).toHaveCount(0);
  await proceed(page, 'Looks right');
  await proceed(page);
  await proceed(page);
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([{project: '~/vibesys-projects/stack', task: 'mpmc'}]);
});

test('your own project: a recent one, its task, the agent on its machine, and the run starts', async ({
  page,
}) => {
  await open(page);
  await choose(page, 'Start from', 'Your project');
  await proceed(page);
  await expect(question(page, 'Which project?')).toBeVisible();
  await choose(page, 'Recent projects', /queue-rs/);
  await proceed(page);
  await expect(question(page, 'Which task?')).toBeVisible();
  await choose(page, 'Task', 'mpmc');
  await proceed(page);
  // The goal is read when the run starts: there is no copy of it here yet.
  await expect(main(page)).toContainText("VibeSys reads this task's goal when the run starts.");
  await proceed(page, 'Looks right');
  // The project is on host-b, so that is where the agent works.
  await expect(main(page).getByRole('radio', {name: /This Mac/})).toBeDisabled();
  await expect(main(page)).toContainText('Your project is on host-b.');
  await proceed(page);
  await proceed(page);
  await expect(main(page)).toContainText(/Ready · \d+ checks passed/);
  expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  await expect(page.getByText('host-b · 20261015-200000')).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toEqual([
    {
      project: PROJECT,
      task: 'mpmc',
      harness: {provider: 'claude', model: 'claude-opus-4-5'},
      options: {results: 'keep', roles: []},
      agents: {},
      args: [],
    },
  ]);
});

test('the steps show their answers and go back to them; a later question waits for the earlier', async ({
  page,
}) => {
  await openAt(page, pageNamed('goal LLM'));
  const list = steps(page);
  const items = list.getByRole('button');
  await expect(items.filter({hasText: 'Start from'})).toContainText('An example');
  await expect(items.filter({hasText: 'Domain'})).toContainText('LLM serving');
  await expect(items.filter({hasText: 'Model'})).toContainText('Qwen3.5 9B');
  await expect(items.filter({hasText: 'Hardware'})).toContainText('1× AMD MI210');
  // The goal is open; the steps after it cannot be opened before it is read.
  await expect(items.filter({hasText: 'Where it runs'})).toBeDisabled();
  await expect(items.filter({hasText: 'Review and start'})).toBeDisabled();
  await items.filter({hasText: 'Model'}).click();
  await expect(question(page, 'Which model do you want to serve?')).toBeVisible();
  await expect(items.filter({hasText: 'Model'})).toHaveAttribute('aria-current', 'page');
  // Choosing another model clears the hardware that followed it.
  await choose(page, 'Meta', /Llama 3 8B/);
  await expect(items.filter({hasText: 'Hardware'})).not.toContainText('MI210');
  // The goal cannot be reached before the hardware it depends on is chosen.
  await expect(items.filter({hasText: 'Goal'})).toBeDisabled();
  await proceed(page);
  await expect(main(page).getByRole('button', {name: 'Continue'})).toHaveAttribute(
    'aria-disabled',
    'true',
  );
});

test('changing the domain clears the questions of the old one, and Back returns a page', async ({
  page,
}) => {
  await openAt(page, pageNamed('goal data structure'));
  await steps(page)
    .getByRole('button', {name: /Domain/})
    .click();
  await choose(page, 'Domain', 'Databases');
  await proceed(page);
  await expect(question(page, 'Which example?')).toBeVisible();
  await expect(steps(page).getByRole('button', {name: /Structure/})).toHaveCount(0);
  await main(page).getByRole('button', {name: 'Back'}).click();
  await expect(question(page, 'Which domain?')).toBeVisible();
});

test('an unfinished run is a draft: it is listed in the sidebar, kept across a reload, and discarded', async ({
  page,
}) => {
  await open(page);
  const sidebar = page.getByRole('navigation', {name: 'VibeSys'});
  await expect(sidebar.getByText('Draft', {exact: true})).toHaveCount(0);
  await choose(page, 'Start from', 'An example');
  await proceed(page);
  await choose(page, 'Domain', 'LLM serving');
  await proceed(page);
  const draft = sidebar.getByRole('region', {name: 'Draft'});
  await expect(draft).toContainText('3 of 8 · Model');
  // Leaving New run and coming back finds it where it was left.
  await sidebar.getByRole('button', {name: 'Settings'}).click();
  await expect(question(page, 'Which model do you want to serve?')).toHaveCount(0);
  await draft.getByRole('button', {name: 'LLM serving 3 of 8 \u00b7 Model'}).click();
  await expect(question(page, 'Which model do you want to serve?')).toBeVisible();
  await page.reload();
  await expect(question(page, 'Which model do you want to serve?')).toBeVisible();
  await expect(steps(page).getByRole('button', {name: /Domain/})).toContainText('LLM serving');
  await draft.getByRole('button', {name: /More for/}).click();
  await page.getByRole('menuitem', {name: 'Discard draft'}).click();
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await expect(sidebar.getByRole('region', {name: 'Draft'})).toHaveCount(0);
});

test('Start over forgets the answers', async ({page}) => {
  await openAt(page, pageNamed('goal LLM'));
  await steps(page).getByRole('button', {name: 'Start over'}).click();
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await expect(steps(page).getByRole('button', {name: 'Start over'})).toHaveCount(0);
});

test('a helper without the catalogue lists examples and their tasks, with no domain or goal text', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await open(page, {
    ...SEED,
    newRun: {
      ...newRun,
      examples: exampleCatalogueWithoutDetails,
      features: ['run_options', 'preflight', 'examples', 'run_roles'],
    },
  });
  await choose(page, 'Start from', 'An example');
  await proceed(page);
  await expect(question(page, 'Which example?')).toBeVisible();
  await choose(page, 'Example', /olmo-hybrid-prefix-caching/);
  await proceed(page);
  await expect(question(page, 'Which task?')).toBeVisible();
  await choose(page, 'Task', 'vllm');
  await proceed(page);
  await expect(main(page)).toContainText('It is not shown here yet.');
});

test('a machine with no examples dims that start, and says so', async ({page}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await open(page, {
    ...SEED,
    newRun: {...newRun, examples: {...newRun.examples, examples: []}},
  });
  const example = main(page).getByRole('radio', {name: /An example/});
  await expect(example).toBeDisabled();
  await expect(main(page)).toContainText('No examples on host-b.');
});

test('a machine whose VibeSys predates New run says so and is never asked to start', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('review project'), {
    ...SEED,
    newRun: {...newRun, features: ['harnesses']},
  });
  await expect(main(page)).toContainText(
    'VibeSys on host-b is older than this app: update its checkout to start runs from here ' +
      '(it lacks run_options, preflight, examples).',
  );
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await start(page).click({force: true});
  const {started, preflights} = await fakeNewRun(page);
  expect(started).toEqual([]);
  expect(preflights).toEqual([]);
});

test('Run again opens Review with the run as it was started', async ({page}) => {
  const run = {
    harness: {provider: 'claude', model: 'claude-sonnet-4-5'},
    options: {rounds: 4, time_limit_s: null, results: 'keep', roles: []},
    agents: {},
    args: ['--max-in-flight', '3'],
  };
  const recent = (SEED.recent ?? []).map(each =>
    (each as {runId: string}).runId === '20261014-090000'
      ? {...(each as object), start: run}
      : each,
  );
  const seed = {...SEED, recent};
  await page.addInitScript(installFakeBridge, seed);
  await page.addInitScript(installFakeMachines, seed);
  await page.addInitScript(installFakeNewRun, seed);
  await page.addInitScript(installFakeRun, {streams: [], text: {}});
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: /^\S+ queue-rs · spsc/})
    .click();
  await page.getByRole('button', {name: 'Run again', exact: true}).click();
  await expect(question(page, 'Ready to start?')).toBeVisible();
  await expect(main(page)).toContainText('Sonnet 4.5 · Claude Code');
  await expect(main(page)).toContainText('Ideas to try');
  await expect(main(page)).toContainText('4');
  // Extra arguments are in More options, which says they are set while it is closed.
  await expect(main(page).getByRole('button', {name: /More options/})).toContainText(
    'extra arguments',
  );
  await main(page).getByRole('button', {name: 'More options'}).click();
  await expect(main(page).getByLabel('Extra arguments')).toHaveValue('--max-in-flight 3');
});

test('a project inside another repository is copied out by its fix, then may start', async ({
  page,
}) => {
  await open(page);
  await choose(page, 'Start from', 'Your project');
  await proceed(page);
  await main(page).getByLabel('Folder on host-b').fill('/home/me/vibesys/examples/queue-rs');
  await main(page).getByRole('button', {name: 'Use this folder'}).click();
  await proceed(page);
  await choose(page, 'Task', 'mpmc');
  await proceed(page);
  await proceed(page, 'Looks right');
  await proceed(page);
  await proceed(page);
  const copy = main(page).getByRole('button', {name: 'Copy to its own repository'});
  await expect(copy).toBeVisible();
  await expect(main(page)).toContainText('Its copy goes to /home/me/vibesys-projects/queue-rs-2.');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(main(page)).toContainText('Fix the items marked above to start.');
  await copy.click();
  await expect(copy).toHaveCount(0);
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([{project: '/home/me/vibesys-projects/queue-rs-2', task: 'mpmc'}]);
});

test('the folder field completes paths on the machine', async ({page}) => {
  await open(page);
  await choose(page, 'Start from', 'Your project');
  await proceed(page);
  const field = main(page).getByLabel('Folder on host-b');
  await field.fill('/home/me/pro');
  await page.getByRole('option', {name: '/home/me/projects/'}).click();
  await expect(field).toHaveValue('/home/me/projects/');
  await page.getByRole('option', {name: '/home/me/projects/queue-rs/'}).click();
  await expect(field).toHaveValue('/home/me/projects/queue-rs/');
  await main(page).getByRole('button', {name: 'Use this folder'}).click();
  await proceed(page);
  await expect(question(page, 'Which task?')).toBeVisible();
  await expect(main(page).getByRole('radio', {name: 'mpmc'})).toBeVisible();
});

test('extra arguments may not repeat an option set above, and roles change the run', async ({
  page,
}) => {
  await openAt(page, pageNamed('review project'));
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await main(page).getByRole('button', {name: 'More options'}).click();
  await main(page).getByLabel('Extra arguments').fill('--max-rounds 3');
  await expect(main(page).getByText('--max-rounds is set above')).toHaveCount(2);
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await main(page).getByLabel('Extra arguments').fill('--max-in-flight 3');
  await main(page).getByLabel('Time limit').selectOption('3600');
  await main(page)
    .getByRole('button', {name: /^Reasoning effort:/})
    .click();
  await main(page).getByLabel('Judge reasoning effort').selectOption('high');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([
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

test('a failing check lists itself first, and the error stays when More options is closed', async ({
  page,
}) => {
  await openAt(page, pageNamed('review project'));
  await main(page).getByRole('button', {name: 'More options'}).click();
  await main(page).getByLabel('Extra arguments').fill('--max-rounds 3');
  await main(page).getByRole('button', {name: 'More options'}).click();
  await expect(main(page).getByLabel('Extra arguments')).toHaveCount(0);
  await expect(main(page).getByRole('button', {name: /More options/})).toContainText(
    '--max-rounds is set above',
  );
  await expect(main(page)).toContainText('1 check needs attention');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
});

test('a machine whose VibeSys does not report its roles says every role runs as the run', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('review project'), {
    ...SEED,
    newRun: {...newRun, features: ['run_options', 'preflight', 'examples']},
  });
  await main(page).getByRole('button', {name: 'More options'}).click();
  await expect(page.getByText(/does not report its agent roles/)).toBeVisible();
  await expect(main(page).getByRole('button', {name: /^Reasoning effort:/})).toHaveCount(0);
});

test('an agent for a role: closed until opened, chosen from the run harness, listed on Review, sent as agents', async ({
  page,
}) => {
  await openAt(page, pageNamed('agent project'));
  const toggle = main(page).getByRole('button', {name: /Use a different agent for a role/});
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toContainText("Every role uses the run's agent");
  await toggle.click();
  const judge = main(page).getByLabel('Judge model');
  await expect(judge).toHaveValue('');
  // Models sit under their maker and family; the run's own harness is the only one a role may use.
  await expect(judge.locator('optgroup')).toHaveAttribute('label', 'Anthropic \u00b7 Claude');
  await expect(main(page).getByLabel('Judge harness')).toHaveCount(0);
  await expect(main(page)).toContainText("every role uses the run's harness");
  await judge.selectOption({label: 'Sonnet 4.5'});
  await expect(judge).not.toHaveValue('');
  // The run's own model is the same as no choice: the role follows the run again.
  await judge.selectOption({label: 'Opus 4.5'});
  await expect(judge).toHaveValue('');
  await judge.selectOption({label: 'Sonnet 4.5'});
  await main(page).getByRole('button', {name: 'Continue'}).click();
  await expect(main(page)).toContainText('Judge agent');
  await expect(main(page)).toContainText('Sonnet 4.5 \u00b7 Claude Code');
  await main(page)
    .getByText('Judge agent', {exact: true})
    .locator('xpath=ancestor::*[.//button][1]')
    .getByRole('button', {name: 'Change'})
    .click();
  await expect(main(page).getByLabel('Judge model')).toBeVisible();
  await main(page).getByRole('button', {name: 'Continue'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([
    {
      agents: {'multi-judge': {provider: 'claude', model: 'claude-sonnet-4-5'}},
      options: {roles: []},
    },
  ]);
});

test('a machine whose VibeSys cannot run an agent per role says so on the Agent page', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('agent project'), {
    ...SEED,
    newRun: {...newRun, features: ['run_options', 'preflight', 'examples', 'run_roles']},
  });
  await expect(main(page).getByText(/cannot run a different agent for a role/)).toBeVisible();
  await expect(main(page).getByRole('button', {name: /different agent for a role/})).toHaveCount(0);
});

test('More options starts closed, says what it holds, and stays as left on each machine', async ({
  page,
}) => {
  await openAt(page, pageNamed('review project'));
  const more = main(page).getByRole('button', {name: /More options/});
  await expect(more).toHaveAttribute('aria-expanded', 'false');
  await expect(more).toContainText('Defaults');
  await more.click();
  await expect(main(page).getByLabel('Extra arguments')).toBeVisible();
  await page.reload();
  await expect(main(page).getByRole('button', {name: /More options/})).toHaveAttribute(
    'aria-expanded',
    'true',
  );
});

test('the harness is chosen from tiles with a mark and a name each', async ({page}) => {
  await openAt(page, pageNamed('agent'));
  const harness = main(page).getByRole('radiogroup', {name: 'Harness'});
  await expect(harness.getByRole('radio')).toHaveCount(1);
  await expect(harness.locator('svg.vs-brand-mark')).toHaveCount(1);
  await expect(harness).toContainText('Installed on host-b');
  await expect(
    main(page).getByRole('radiogroup', {name: 'Model line'}).getByRole('radio'),
  ).toHaveCount(2);
  await choose(page, 'Model line', 'OpenAI');
  await expect(main(page).getByLabel('Model', {exact: true})).toHaveValue('gpt-5-codex');
  await expect(main(page).getByLabel('Model', {exact: true}).locator('option:checked')).toHaveText(
    'GPT-5 Codex',
  );
  await expect(main(page).getByRole('radiogroup', {name: 'Harness'})).toContainText('Codex');
});

test("Machines' New run here opens New run on that machine", async ({page}) => {
  await openAt(page, pageNamed('where'));
  await expect(
    main(page)
      .getByRole('radiogroup', {name: 'Agent works on'})
      .getByRole('radio', {name: /host-b/}),
  ).toBeChecked();
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'Settings'})
    .click();
  await page
    .getByRole('navigation', {name: 'Settings sections'})
    .getByRole('button', {name: /Machines/})
    .click();
  await page
    .getByRole('tree', {name: 'Machines'})
    .getByRole('treeitem', {name: 'This Mac'})
    .click();
  await page.getByRole('button', {name: 'New run here'}).click();
  await expect(question(page, /Where does the agent work/)).toBeVisible();
  await expect(
    main(page)
      .getByRole('radiogroup', {name: 'Agent works on'})
      .getByRole('radio', {name: /This Mac/}),
  ).toBeChecked();
});

test('a bridge call that throws is worded plainly, names what failed, and Try again reloads it', async ({
  page,
}) => {
  await page.addInitScript(installFakeBridge, SEED);
  await page.addInitScript(installFakeMachines, SEED);
  await page.addInitScript(installFakeNewRun, SEED);
  // The agents' request is missing, as a bridge built before it would be.
  await page.addInitScript(() => {
    const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
    (window as unknown as {restoreHarnesses: () => void}).restoreHarnesses = (() => {
      const real = bridge['harnesses'];
      delete bridge['harnesses'];
      return () => {
        bridge['harnesses'] = real;
      };
    })();
  });
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'New run'})
    .click();

  await expect(page.getByText(/Could not read the agents .* runs\./)).toBeVisible();
  await expect(page.getByText('Something unexpected happened.', {exact: false})).toBeVisible();
  await expect(page.getByText(/is not a function/)).toHaveCount(0);

  await page.evaluate(() =>
    (window as unknown as {restoreHarnesses: () => void}).restoreHarnesses(),
  );
  await page.getByRole('button', {name: 'Try again'}).click();
  await expect(page.getByRole('button', {name: 'Try again'})).toHaveCount(0);
});

for (const each of NEW_RUN_PAGES) {
  test(`the ${each.name} page draws no role, harness, model or example ids as raw text`, async ({
    page,
  }) => {
    await openAt(page, each);
    await expect(main(page).getByRole('heading', {level: 1})).toBeVisible();
    expect(await rawTextOf(page)).toEqual([]);
  });
}

test('models by maker: the latest generation is shown and older ones wait behind Older (n)', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
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
  await openAt(page, pageNamed('agent'), {
    ...SEED,
    newRun: {
      ...newRun,
      harnesses: [
        offer('gpt-6-luna', 1, 'Luna', 1),
        offer('gpt-6-sol', 2, 'Sol', 1),
        offer('gpt-5-luna', 1, 'Luna', 2),
        offer('gpt-5-sol', 2, 'Sol', 2),
      ],
    },
  });
  await expect(main(page).getByRole('radio', {name: 'OpenAI'})).toBeChecked();
  const generation = main(page).getByRole('radiogroup', {name: 'Generation'});
  await expect(generation.getByRole('radio')).toHaveCount(1);
  await expect(generation).toContainText('GPT-6 (latest)');
  await main(page).getByRole('button', {name: 'Older (1)'}).click();
  await expect(generation.getByRole('radio')).toHaveCount(2);
  await generation.getByRole('radio', {name: 'GPT-5'}).check();
  // The tier the reader was on is kept in the older generation.
  await expect(main(page).getByRole('slider', {name: 'Model'})).toHaveAttribute(
    'aria-valuetext',
    'Sol',
  );
  await expect(main(page).getByText('GPT-5-SOL', {exact: true})).toBeVisible();
  await expect(main(page).getByRole('button', {name: /Older/})).toHaveCount(0);
});
