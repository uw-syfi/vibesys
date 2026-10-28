import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import type {ExampleList} from '@vibesys/contracts/host-helper';
import {REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import {
  exampleCatalogue,
  exampleCatalogueWithoutDetails,
  queueRsCatalogue,
  storeRunFixture,
} from '@vibesys/run-fixtures';
import {rawTextOf} from './checks/screen-check.js';
import {settled} from './checks/settle.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {installGateway, wire} from './fake-gateway.js';
import {installFakeRun} from './fake-run.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {
  choose,
  exampleRow,
  main,
  NEW_RUN_PAGES,
  openAt,
  pickExample,
  proceed,
  question,
  steps,
} from './new-run-flow.js';
import {type FakeHostWindow, openNewRun as open, PROJECT, SEED} from './world.js';

const start = (page: Page) => main(page).getByRole('button', {name: 'Start run'});
const continueButton = (page: Page) =>
  main(page).getByRole('button', {name: 'Continue', exact: true});
const beforeYouStart = (page: Page) => main(page).locator('.vs-check-list');

async function fakeNewRun(page: Page) {
  return page.evaluate(() => (window as unknown as FakeHostWindow).fakeHost.newRun);
}

function pageNamed(name: string) {
  const found = NEW_RUN_PAGES.find(each => each.name === name);
  if (found === undefined) throw new Error(`no New run page named ${name}`);
  return found;
}

/** The world's seed with New run's helper changed by `change`. */
function withNewRun(
  change: (newRun: NonNullable<FakeBridgeSeed['newRun']>) => NonNullable<FakeBridgeSeed['newRun']>,
  seed: FakeBridgeSeed = SEED,
): FakeBridgeSeed {
  const newRun = seed.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  return {...seed, newRun: change(newRun)};
}

test('an example of LLM serving: picked from the table, its goal on Review, then the run starts', async ({
  page,
}) => {
  await open(page);
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await expect(main(page).getByText('New run · step 1 of 2')).toBeVisible();
  await choose(page, 'Start from', 'Examples');
  await choose(page, 'Domain', /^LLM serving/);
  await expect(exampleRow(page, 'stack', 'mpmc')).toHaveCount(0);
  const qwen = exampleRow(page, 'qwen3.5-9b-mi210');
  // A row says what the task is and what it needs, from the catalogue.
  await expect(qwen).toContainText('Serve Qwen3.5 9B');
  await expect(qwen).toContainText('1× AMD MI210');
  await expect(continueButton(page)).toHaveAttribute('aria-disabled', 'true');
  await qwen.click();
  await expect(qwen).toHaveAttribute('aria-current', 'true');
  await expect(page.getByText('qwen3.5-9b-mi210', {exact: true}).last()).toBeVisible();
  await proceed(page);

  // Review: the goal and what it optimizes, every other answer filled in and changed in place.
  await expect(question(page, 'Review and start')).toBeVisible();
  await expect(main(page)).toContainText('Higher is better');
  await expect(main(page)).toContainText('output_token_throughput_per_s');
  await expect(main(page)).toContainText('Lower is better');
  await expect(main(page)).toContainText('p90_latency_ms');
  await main(page).getByRole('button', {name: 'Read OBJECTIVE.md'}).click();
  await expect(
    main(page).getByText('Objective: Qwen3.5-9B agentic-coding-session serving'),
  ).toBeVisible();
  await expect(main(page).getByLabel('Agent works on')).toHaveValue('ssh:host-b');
  await expect(main(page).getByLabel('Model', {exact: true})).toHaveValue(
    JSON.stringify(['claude', 'claude-opus-4-5']),
  );
  await main(page).getByLabel('Model', {exact: true}).selectOption({label: 'Sonnet 4.5'});
  await main(page).getByLabel('Ideas to try').selectOption('3');

  // The example is not a project until it is made.
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
      measure_on: {kind: 'same_machine'},
      args: [],
    },
  ]);
});

test('an example data structure: a goal with no direction made up', async ({page}) => {
  await open(page);
  await pickExample(page, 'stack', 'mpmc');
  await expect(
    main(page).getByText('Optimize a multi-producer, multi-consumer bounded stack.'),
  ).toBeVisible();
  // The task declares no objectives: no direction is shown for its metric.
  await expect(main(page).getByText(/Higher is better|Lower is better/)).toHaveCount(0);
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([{project: '~/vibesys-projects/stack', task: 'mpmc'}]);
});

test('an example of the real catalogue shows its goal like a project of the user: the text, what it optimizes, the file', async ({
  page,
}) => {
  await open(
    page,
    withNewRun(newRun => ({...newRun, examples: queueRsCatalogue})),
  );
  await pickExample(page, 'queue-rs', 'mpmc');
  await expect(main(page)).toContainText(
    'Optimize a multi-producer, multi-consumer bounded FIFO queue',
  );
  await expect(main(page)).toContainText('Higher is better');
  await expect(main(page)).toContainText('total_ops_per_sec');
  await main(page).getByRole('button', {name: 'Read OBJECTIVE.md'}).click();
  await expect(main(page)).toContainText('.vibesys/tasks/mpmc/OBJECTIVE.md');
});

test('your own project: a recent one, its task, the agent on its machine, and the run starts', async ({
  page,
}) => {
  await open(page);
  // A user with recent projects starts on them.
  await expect(
    main(page)
      .getByRole('radiogroup', {name: 'Start from'})
      .getByRole('radio', {name: /^Your projects/}),
  ).toBeChecked();
  await choose(page, 'Recent projects', /queue-rs/);
  await choose(page, 'Task', 'mpmc');
  await proceed(page);
  // The goal is the task's own OBJECTIVE.md, read from the machine, with the axes it declares.
  await expect(main(page)).toContainText(
    'Raise throughput on the mpmc workload without breaking linearizability.',
  );
  await expect(main(page)).toContainText('Higher is better');
  await expect(main(page)).toContainText('ops_per_second');
  await expect(main(page)).toContainText('Lower is better');
  // The project is on host-b, so that is where the agent works: nothing to choose.
  await expect(main(page).getByRole('combobox', {name: 'Agent works on'})).toHaveCount(0);
  await expect(main(page)).toContainText('Where the project is');
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
      measure_on: {kind: 'same_machine'},
      args: [],
    },
  ]);
});

test('a machine whose VibeSys cannot read a goal says Needs an update, not that the goal is hidden', async ({
  page,
}) => {
  await openAt(
    page,
    pageNamed('review project'),
    withNewRun(newRun => ({
      ...newRun,
      features: [...REQUIRED_HELPER_FEATURES, 'run_roles', 'example_catalogue'],
    })),
  );
  await expect(main(page)).toContainText('Needs an update');
  await expect(main(page)).not.toContainText('Goal not shown here');
  await expect(main(page)).not.toContainText('Higher is better');
});

/** The example catalogue with `extra` models served on an NVIDIA L4. */
function longCatalogue(extra: number): ExampleList {
  const base = exampleCatalogue.examples;
  const qwen = base.find(each => each.name === 'model-serving/qwen3.5-9b-mi210');
  if (qwen === undefined) throw new Error('the fixture has no Qwen example');
  const many = Array.from({length: extra}, (_, index) => ({
    ...qwen,
    name: `model-serving/long-${index}`,
    task_details: (qwen.task_details ?? []).map(task => ({
      ...task,
      model: {name: `Model ${index}`, maker: 'other' as const},
      hardware: {
        accelerator: 'NVIDIA L4',
        count: 1,
        vendor: 'nvidia' as const,
        backend: 'cuda' as const,
      },
    })),
  }));
  return {...exampleCatalogue, examples: [...base, ...many]};
}

type Box = {x: number; y: number; width: number; height: number};

/** The footer's Back (on Review only) and its way on, as they are now. */
async function footer(page: Page, review: boolean): Promise<{back: Box | null; next: Box | null}> {
  const boxOf = async (name: string) =>
    main(page).getByRole('button', {name, exact: true}).boundingBox();
  return {
    back: review ? await boxOf('Back') : null,
    next: await boxOf(review ? 'Start run' : 'Continue'),
  };
}

function expectInside(box: Box | null, width: number, height: number): void {
  expect(box).not.toBeNull();
  expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
  expect(box?.y ?? -1).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(width);
  expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(height);
}

for (const width of [1440, 1000, 660]) {
  for (const height of [900, 480]) {
    test(`Continue and Start run stay inside the ${width}x${height} window, in one place, whatever the content does`, async ({
      page,
    }) => {
      await page.setViewportSize({width, height});
      const seed = withNewRun(newRun => ({...newRun, examples: longCatalogue(40)}));
      const edges: unknown[] = [];
      const backs: unknown[] = [];
      for (const name of ['pick examples', 'pick projects', 'review LLM', 'review project']) {
        await openAt(page, pageNamed(name), seed);
        // Geometry is meaningful after font swapping and the rendered layout have settled.
        await settled(page);
        expect(page.viewportSize()).toEqual({width, height});
        const review = name.startsWith('review');
        const before = await footer(page, review);
        if (review) expectInside(before.back, width, height);
        expectInside(before.next, width, height);
        const region = main(page).locator('.vs-page-scroll');
        if (name === 'pick examples') {
          // The long table outgrows the window, so it scrolls in its own region.
          expect(
            await region.evaluate(element => element.scrollHeight > element.clientHeight),
          ).toBe(true);
        }
        await region.evaluate(element => element.scrollTo(0, element.scrollHeight));
        expect(await footer(page, review)).toEqual(before);
        if (review) backs.push(before.back);
        // Each way on is as wide as its words; the edge it sits on is the same.
        const next = before.next;
        edges.push({y: next?.y, height: next?.height, right: (next?.x ?? 0) + (next?.width ?? 0)});
      }
      for (const each of backs) expect(each).toEqual(backs[0]);
      for (const each of edges) expect(each).toEqual(edges[0]);
    });
  }
}

test('the examples table: a domain filter and a search narrow it, and Continue waits for a row', async ({
  page,
}) => {
  await openAt(page, pageNamed('pick examples'));
  const rows = main(page).getByRole('row');
  await choose(page, 'Domain', /^Data structures/);
  await expect(rows.filter({hasText: 'qwen3.5-9b-mi210'})).toHaveCount(0);
  await expect(exampleRow(page, 'stack', 'mpmc')).toHaveCount(1);
  await choose(page, 'Domain', /^All/);
  await main(page).getByLabel('Search examples').fill('MI210');
  await expect(rows.filter({hasText: 'qwen3.5-9b-mi210'})).toHaveCount(1);
  await expect(rows.filter({hasText: 'stack'})).toHaveCount(0);
  // A blocked Continue draws no caption.
  await expect(continueButton(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(main(page).getByText('Choose a task first.')).toBeHidden();
  await rows.filter({hasText: 'qwen3.5-9b-mi210'}).press('Enter');
  await expect(continueButton(page)).not.toHaveAttribute('aria-disabled', 'true');
});

test('the steps show their answers and go back to them; Review waits for a pick', async ({
  page,
}) => {
  await openAt(page, pageNamed('review LLM'));
  const items = steps(page).getByRole('button');
  const pick = items.filter({hasText: 'Choose what to optimize'});
  await expect(pick).toContainText('qwen3.5-9b-mi210');
  await expect(items.filter({hasText: 'Review and start'})).toHaveAttribute('aria-current', 'page');
  await pick.click();
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await expect(exampleRow(page, 'qwen3.5-9b-mi210')).toHaveAttribute('aria-current', 'true');
  await proceed(page);
  await main(page).getByRole('button', {name: 'Change', exact: true}).click();
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await steps(page).getByRole('button', {name: 'Start over'}).click();
  await expect(items.filter({hasText: 'Review and start'})).toBeDisabled();
  await expect(steps(page).getByRole('button', {name: 'Start over'})).toHaveCount(0);
});

for (const width of [1440, 1000, 660]) {
  test(`a step has its number in a neutral circle, the pick made a tick, no status colour, at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    await openAt(page, pageNamed('review LLM'));
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    const markers = await page.evaluate(() =>
      [...document.querySelectorAll('.vs-sidebar-marker')].map(marker => ({
        text: marker.textContent,
        state: marker.getAttribute('data-state'),
        font: getComputedStyle(marker).fontFamily,
        dots: marker.closest('.vs-sidebar-item')?.querySelectorAll('.vs-dot').length,
      })),
    );
    expect(markers.map(each => each.text)).toEqual(['✓', '2']);
    expect(markers.map(each => each.state)).toEqual(['neutral', 'current']);
    // The collapsed control (shown when narrow) counts the same steps.
    await expect(page.getByText('2 of 2 · Review and start').first()).toBeAttached();
    expect(markers.every(each => each.font.includes('JetBrains Mono'))).toBe(true);
    expect(markers.every(each => each.dots === 0)).toBe(true);
  });
}

test('every step answer fits its line at 1440px', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 900});
  await openAt(page, pageNamed('review roles'));
  const slack = await page.evaluate(() =>
    [...document.querySelectorAll('.vs-sidebar-text > *')].map(line => {
      const range = document.createRange();
      range.selectNodeContents(line);
      const box = line.getBoundingClientRect();
      return {text: line.textContent, slack: box.width - range.getBoundingClientRect().width};
    }),
  );
  expect(slack.length).toBeGreaterThan(0);
  for (const each of slack) expect(each.slack, each.text ?? '').toBeGreaterThanOrEqual(0);
});

test('an unfinished run is a draft: it is offered on the Runs dashboard, kept across a reload, and discarded', async ({
  page,
}) => {
  await open(page);
  const rail = page.getByRole('navigation', {name: 'VibeSys'});
  const home = async (): Promise<void> => {
    await rail.getByRole('button', {name: /^Runs/}).click();
    await expect(page.getByRole('heading', {name: 'Runs'})).toBeVisible();
  };
  await home();
  await expect(main(page).getByText('Draft', {exact: true})).toHaveCount(0);
  await rail.getByRole('button', {name: 'New', exact: true}).click();
  await pickExample(page, 'stack', 'mpmc');
  // Leaving New run and coming back finds it where it was left.
  await home();
  await expect(main(page)).toContainText('stack · mpmc');
  await expect(main(page)).toContainText('2 of 2 · Review and start');
  await main(page).getByRole('button', {name: 'Open'}).click();
  await expect(question(page, 'Review and start')).toBeVisible();
  await page.reload();
  await home();
  await main(page).getByRole('button', {name: 'Open'}).click();
  await expect(question(page, 'Review and start')).toBeVisible();
  await expect(steps(page).getByRole('button', {name: /Choose what to optimize/})).toContainText(
    'stack · mpmc',
  );
  await home();
  await main(page).getByRole('button', {name: 'Discard'}).click();
  await expect(main(page).getByText('Draft', {exact: true})).toHaveCount(0);
  await rail.getByRole('button', {name: 'New', exact: true}).click();
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
});

test('a helper without the catalogue lists examples and their tasks, with no domain or goal text', async ({
  page,
}) => {
  await open(
    page,
    withNewRun(newRun => ({
      ...newRun,
      examples: exampleCatalogueWithoutDetails,
      features: [...REQUIRED_HELPER_FEATURES, 'run_roles'],
    })),
  );
  await choose(page, 'Start from', 'Examples');
  await expect(main(page).getByRole('radiogroup', {name: 'Domain'})).toHaveCount(0);
  await expect(main(page)).not.toContainText('Serve ');
  await pickExample(page, 'olmo-hybrid-prefix-caching', 'vllm');
  await expect(main(page)).toContainText('Needs an update');
  await expect(main(page)).not.toContainText('Goal not shown here');
});

test('a machine with no examples says so on Examples', async ({page}) => {
  await open(
    page,
    withNewRun(newRun => ({...newRun, examples: {...newRun.examples, examples: []}})),
  );
  await choose(page, 'Start from', 'Examples');
  await expect(main(page)).toContainText('No examples');
});

test('a machine whose VibeSys predates New run says so and is never asked to start', async ({
  page,
}) => {
  await openAt(
    page,
    pageNamed('review project'),
    withNewRun(newRun => ({...newRun, features: ['harnesses']})),
  );
  await expect(beforeYouStart(page)).toContainText('VibeSys on host-b');
  await expect(beforeYouStart(page)).toContainText('Needs an update');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await start(page).click({force: true});
  const {started, preflights} = await fakeNewRun(page);
  expect(started).toEqual([]);
  expect(preflights).toEqual([]);
});

test('a machine that serves everything but the run trace says Needs an update and never starts', async ({
  page,
}) => {
  await openAt(
    page,
    pageNamed('review project'),
    withNewRun(newRun => ({
      ...newRun,
      features: REQUIRED_HELPER_FEATURES.filter(feature => feature !== 'run_trace'),
    })),
  );
  await expect(beforeYouStart(page)).toContainText('VibeSys on host-b');
  await expect(beforeYouStart(page)).toContainText('Needs an update');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await start(page).click({force: true});
  const {started} = await fakeNewRun(page);
  expect(started).toEqual([]);
});

test('Run again opens Review with the run as it was started', async ({page}) => {
  const run = {
    harness: {provider: 'claude', model: 'claude-sonnet-4-5'},
    options: {rounds: 4, time_limit_s: null, results: 'keep', roles: []},
    agents: {},
    measure_on: {kind: 'same_machine'},
    args: ['--max-in-flight', '3'],
  };
  const recent = (SEED.recent ?? []).map(each =>
    (each as {runId: string}).runId === '20261014-090000'
      ? {...(each as object), start: run}
      : each,
  );
  const seed = {...SEED, recent};
  await addInitScripts(page, [
    initScript(installFakeBridge, seed),
    initScript(installFakeProjectRuns, seed),
    initScript(installFakeMachines, seed),
    initScript(installFakeNewRun, seed),
    initScript(installFakeRun, {streams: [], text: {}}),
  ]);
  // A recent run is a finished one: the page offers Run again.
  await installGateway(page, wire(storeRunFixture('short')));
  await page.goto('./shell.html');
  await page
    .getByRole('main')
    .getByRole('button', {name: /^queue-rs · spsc/})
    .click();
  await page.getByRole('button', {name: 'Run again', exact: true}).click();
  await expect(question(page, 'Review and start')).toBeVisible();
  await expect(main(page).getByLabel('Model', {exact: true}).locator('option:checked')).toHaveText(
    'Sonnet 4.5',
  );
  await expect(main(page).getByLabel('Ideas to try')).toHaveValue('4');
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
  await main(page).getByRole('button', {name: 'Open a folder…'}).click();
  await main(page).getByLabel('Folder on host-b').fill('/home/me/vibesys/examples/queue-rs');
  await main(page).getByRole('button', {name: 'Use this folder'}).click();
  await choose(page, 'Task', 'mpmc');
  await proceed(page);
  const copy = main(page).getByRole('button', {name: 'Copy to its own repository'});
  await expect(copy).toBeVisible();
  await expect(main(page)).toContainText('Its copy goes to /home/me/vibesys-projects/queue-rs-2.');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(main(page)).toContainText('Fix the check under Before you start.');
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
  await main(page).getByRole('button', {name: 'Open a folder…'}).click();
  const field = main(page).getByLabel('Folder on host-b');
  await field.fill('/home/me/pro');
  await page.getByRole('option', {name: '/home/me/projects/'}).click();
  await expect(field).toHaveValue('/home/me/projects/');
  await page.getByRole('option', {name: '/home/me/projects/queue-rs/'}).click();
  await expect(field).toHaveValue('/home/me/projects/queue-rs/');
  await main(page).getByRole('button', {name: 'Use this folder'}).click();
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
  await expect(main(page)).toContainText('1 check needs you');
  await expect(beforeYouStart(page).locator('li').first()).toContainText(
    '--max-rounds is set above',
  );
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
});

test('a machine whose VibeSys does not report its roles says every role runs as the run', async ({
  page,
}) => {
  await openAt(
    page,
    pageNamed('review project'),
    withNewRun(newRun => ({...newRun, features: REQUIRED_HELPER_FEATURES})),
  );
  await main(page).getByRole('button', {name: 'More options'}).click();
  await expect(page.getByText(/does not report its agent roles/)).toBeVisible();
  await expect(main(page).getByRole('button', {name: /^Reasoning effort:/})).toHaveCount(0);
});

test('an agent for a role: closed until opened, chosen from the run harness, sent as agents', async ({
  page,
}) => {
  await openAt(page, pageNamed('review project'));
  const toggle = main(page).getByRole('button', {name: /Use a different agent for a role/});
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(toggle).toContainText('Same as the run');
  await toggle.click();
  const judge = main(page).getByLabel('Judge model');
  await expect(judge).toHaveValue('');
  // Models sit under their maker and family; the run's own harness is the only one a role may use.
  await expect(judge.locator('optgroup')).toHaveAttribute('label', 'Anthropic · Claude');
  await expect(main(page).getByLabel('Judge harness')).toHaveCount(0);
  await judge.selectOption({label: 'Sonnet 4.5'});
  await expect(judge).not.toHaveValue('');
  // The run's own model is the same as no choice: the role follows the run again.
  await judge.selectOption({label: 'Opus 4.5'});
  await expect(judge).toHaveValue('');
  await judge.selectOption({label: 'Sonnet 4.5'});
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

test('a machine whose VibeSys cannot run an agent per role says Needs an update on that row, never beside an installed harness', async ({
  page,
}) => {
  await openAt(
    page,
    pageNamed('review project'),
    withNewRun(newRun => ({...newRun, features: [...REQUIRED_HELPER_FEATURES, 'run_roles']})),
  );
  // One status for the row that cannot be used; the harness keeps saying it is installed.
  const row = main(page).getByText('Use a different agent for a role').locator('..');
  await expect(row).toContainText('Needs an update');
  await expect(beforeYouStart(page)).toContainText('Claude Code installed on host-b');
  await expect(main(page).getByText('Not supported on this machine')).toHaveCount(0);
  await expect(main(page).getByRole('button', {name: /different agent for a role/})).toHaveCount(0);
});

test('the model is one list across harnesses, and choosing another maker’s model moves the harness', async ({
  page,
}) => {
  await openAt(page, pageNamed('review LLM'));
  const model = main(page).getByLabel('Model', {exact: true});
  await expect(model.locator('optgroup')).toHaveCount(2);
  await expect(main(page)).toContainText('Claude Code');
  await model.selectOption({label: 'GPT-5 Codex'});
  await expect(model).toHaveValue(JSON.stringify(['codex', 'gpt-5-codex']));
  await expect(beforeYouStart(page)).toContainText('Codex installed on host-b');
});

test('Measured on: a machine with nothing set up links to its Settings, and Back returns to Review', async ({
  page,
}) => {
  await openAt(page, pageNamed('review LLM'));
  // Nothing else is set up, so there is nothing to choose.
  await expect(main(page).getByRole('combobox', {name: 'Measured on'})).toHaveCount(0);
  await expect(main(page)).toContainText('The same machine');
  await main(page).getByRole('button', {name: 'Set up a cluster on host-b'}).click();
  await expect(page.getByRole('combobox', {name: 'Machine'})).toHaveValue('ssh:host-b');
  await expect(
    main(page).getByRole('heading', {name: 'Clusters and clouds'}).first(),
  ).toBeVisible();
  await page.getByRole('button', {name: 'Back', exact: true}).click();
  await expect(question(page, 'Review and start')).toBeVisible();
  await expect(main(page).getByLabel('Agent works on')).toHaveValue('ssh:host-b');
});

test('Measured on: only what the machine has set up is offered, and the choice is the measurement target', async ({
  page,
}) => {
  await openAt(page, pageNamed('review LLM'));
  await main(page).getByLabel('Agent works on').selectOption('local');
  const measured = main(page).getByLabel('Measured on');
  await expect(measured.locator('option')).toHaveText([
    'The same machine',
    'Slurm · lab-h100',
    'Slurm · campus',
    'Slurm · cluster-m',
    'Modal',
  ]);
  await expect(main(page).getByRole('button', {name: /^Set up a cluster on/})).toHaveCount(0);
  await measured.selectOption({label: 'Slurm · lab-h100'});
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([{measure_on: {kind: 'slurm', cluster: 'lab-h100'}}]);
});

test('a GPU task the agent machine lacks is a misfit only when results are measured there', async ({
  page,
}) => {
  const review = pageNamed('review LLM');
  await openAt(page, {
    ...review,
    answers: {
      ...review.answers,
      entry: {example: 'model-serving/olmo-hybrid-prefix-caching', task: 'from-scratch'},
    },
    machine: 'local',
  });
  const measured = main(page).getByLabel('Measured on');
  const fit = beforeYouStart(page).getByText('Hardware fits the task');
  await expect(fit).toBeVisible();
  for (const name of ['Slurm · lab-h100', 'Slurm · campus', 'Modal']) {
    await measured.selectOption({label: name});
    await expect(fit).toHaveCount(0);
  }
  await measured.selectOption({label: 'The same machine'});
  await expect(fit).toBeVisible();
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
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'New', exact: true})
    .click();
  await expect(main(page).getByRole('button', {name: /More options/})).toHaveAttribute(
    'aria-expanded',
    'true',
  );
});

test('an agent not signed in on the machine fails a check, closes Start, and its fix opens Agent logins', async ({
  page,
}) => {
  await openAt(page, pageNamed('review signed out'));
  const item = beforeYouStart(page).locator('li', {hasText: 'Claude Code signed in on host-b'});
  await expect(item).toContainText('Installed, but not signed in.');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(main(page)).toContainText('Fix the check under Before you start.');
  await item.getByRole('button', {name: 'Open Agent logins'}).click();
  await expect(page.getByRole('combobox', {name: 'Machine'})).toHaveValue('ssh:host-b');
  await expect(main(page).getByRole('heading', {name: 'Agent logins'}).first()).toBeVisible();
});

test('an agent signed in on the machine is a passed check', async ({page}) => {
  await openAt(page, {...pageNamed('review project'), logins: {claude: 'signed_in'}});
  await expect(beforeYouStart(page)).toContainText('Claude Code signed in on host-b');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
});

test('a machine that cannot tell whether the agent is signed in adds no sign-in check', async ({
  page,
}) => {
  await openAt(page, {...pageNamed('review project'), logins: {claude: 'unknown'}});
  await expect(beforeYouStart(page)).toContainText('Claude Code installed on host-b');
  await expect(beforeYouStart(page)).not.toContainText('signed in on');
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
});

test("Machines' New run here opens New run on that machine", async ({page}) => {
  await openAt(page, pageNamed('review LLM'));
  await expect(main(page).getByLabel('Agent works on')).toHaveValue('ssh:host-b');
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
  await expect(question(page, 'Review and start')).toBeVisible();
  await expect(main(page).getByLabel('Agent works on')).toHaveValue('local');
});

test('a bridge call that throws is worded plainly, names what failed, and Try again reloads it', async ({
  page,
}) => {
  await addInitScripts(page, [
    initScript(installFakeBridge, SEED),
    initScript(installFakeProjectRuns, SEED),
    initScript(installFakeMachines, SEED),
    initScript(installFakeNewRun, SEED),
    // The agents' request is missing, as a bridge built before it would be.
    initScript(() => {
      const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>})
        .vibesysWelcome;
      (window as unknown as {restoreHarnesses: () => void}).restoreHarnesses = (() => {
        const real = bridge['harnesses'];
        delete bridge['harnesses'];
        return () => {
          bridge['harnesses'] = real;
        };
      })();
    }, undefined),
  ]);
  await page.goto('./shell.html');
  await page
    .getByRole('navigation', {name: 'VibeSys'})
    .getByRole('button', {name: 'New', exact: true})
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

test('a machine that cannot list its examples says Failed and lets another machine be chosen', async ({
  page,
}) => {
  await openAt(
    page,
    pageNamed('pick examples'),
    withNewRun(newRun => ({...newRun, examplesFail: ['ssh:host-b']})),
  );
  await expect(main(page).getByText('Failed', {exact: true})).toBeVisible();
  await expect(main(page).getByText('Listing', {exact: true})).toHaveCount(0);
  // The failed pane is replaced by the new machine's listing as the choice lands.
  await main(page)
    .getByRole('radiogroup', {name: 'Machine', exact: true})
    .getByRole('radio', {name: /This Mac/})
    .click({force: true});
  await expect(main(page).getByRole('radiogroup', {name: 'Domain'})).toBeVisible();
  await expect(main(page).getByText('Failed', {exact: true})).toHaveCount(0);
});

test('a fresh profile whose This Mac cannot list examples offers the other machines at once', async ({
  page,
}) => {
  // A fresh profile has no recent run, so the form starts on This Mac, on the examples.
  await open(
    page,
    withNewRun(newRun => ({...newRun, examplesFail: ['local']}), {
      ...SEED,
      recent: [],
      listings: [],
    }),
  );
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  await expect(main(page).getByText('Failed', {exact: true})).toBeVisible();
  const machines = main(page).getByRole('radiogroup', {name: 'Machine', exact: true});
  await expect(machines.getByRole('radio')).toHaveCount(2);
  await expect(machines.getByRole('radio', {name: /This Mac/})).toBeChecked();
  await machines.getByRole('radio', {name: /host-b/}).click({force: true});
  // Its examples are listed, so the table opens.
  await expect(main(page).getByText('Failed', {exact: true})).toHaveCount(0);
  await expect(exampleRow(page, 'stack', 'mpmc')).toBeVisible();
  await expect(machines).toHaveCount(0);
});
