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
  installFakeBridge,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {installGateway, wire} from './fake-gateway.js';
import {installFakeRun} from './fake-run.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {choose, main, NEW_RUN_PAGES, openAt, proceed, question, steps} from './new-run-flow.js';
import {type FakeHostWindow, openNewRun as open, PROJECT, SEED, sidebar} from './world.js';

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
  // A model card names the model only: its hardware is the next question's.
  await expect(main(page).getByRole('radio', {name: /Qwen3\.5 9B/})).not.toContainText('MI210');
  await expect(main(page)).not.toContainText('H100');
  await choose(page, 'Qwen', /Qwen3\.5 9B/);
  await proceed(page);

  // Hardware is grouped by vendor and lists only what this model has an example for.
  await expect(question(page, 'Which hardware are you optimizing for?')).toBeVisible();
  await expect(main(page).getByRole('radiogroup', {name: 'AMD'})).toBeVisible();
  await expect(main(page).getByRole('radiogroup', {name: 'NVIDIA'})).toHaveCount(0);
  await expect(main(page)).not.toContainText('No example yet');
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
  await expect(main(page).getByText('Hardware fits', {exact: true})).toBeVisible();
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
      measure_on: {kind: 'same_machine'},
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

test('an example of the real catalogue shows its goal like a project of the user: the file, the text, what it optimizes', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await open(page, {...SEED, newRun: {...newRun, examples: queueRsCatalogue}});
  await choose(page, 'Start from', 'An example');
  await proceed(page);
  await choose(page, 'Domain', 'Data structures');
  await proceed(page);
  await choose(page, 'Structure', /queue-rs/);
  await proceed(page);
  await choose(page, 'Workload', /Multiple producers, multiple consumers/);
  await proceed(page);
  await expect(main(page)).toContainText('.vibesys/tasks/mpmc/OBJECTIVE.md');
  await expect(main(page)).toContainText(
    'Optimize a multi-producer, multi-consumer bounded FIFO queue',
  );
  await expect(main(page)).toContainText('Higher is better');
  await expect(main(page)).toContainText('total_ops_per_sec');
  await expect(main(page)).not.toContainText('Goal not shown here');
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
  // The goal is the task's own OBJECTIVE.md, read from the machine, with the axes it declares.
  await expect(main(page)).toContainText('Make the mpmc queue faster');
  await expect(main(page)).toContainText('.vibesys/tasks/mpmc/OBJECTIVE.md');
  await expect(main(page)).toContainText('Higher is better');
  await expect(main(page)).toContainText('ops_per_second');
  await expect(main(page)).toContainText('Lower is better');
  await proceed(page, 'Looks right');
  // The project is on host-b, so that is where the agent works.
  await expect(main(page).getByRole('radio', {name: /This Mac/})).toBeDisabled();
  await expect(main(page)).toContainText('Project on host-b');
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
      measure_on: {kind: 'same_machine'},
      args: [],
    },
  ]);
});

test('a machine whose VibeSys cannot read a goal says Needs an update, not that the goal is hidden', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('goal project'), {
    ...SEED,
    newRun: {
      ...newRun,
      features: [...REQUIRED_HELPER_FEATURES, 'run_roles', 'example_catalogue'],
    },
  });
  await expect(main(page)).toContainText('Needs an update');
  await expect(main(page)).toContainText('.vibesys/tasks/mpmc/OBJECTIVE.md');
  await expect(main(page)).not.toContainText('Goal not shown here');
  await expect(main(page)).not.toContainText('Higher is better');
});

for (const width of [1440, 1000, 660]) {
  for (const height of [900, 480]) {
    test(`Back and Continue sit in one place on every step at ${width}px, ${height}px high`, async ({
      page,
    }) => {
      await page.setViewportSize({width, height});
      // Five steps of different lengths: a short question, a list, the hardware tiles, Where
      // (long) and Agent (long); the tall window leaves room, the low one makes them scroll.
      const boxes: {back: unknown; next: unknown}[] = [];
      for (const name of ['domain', 'model', 'hardware', 'where', 'agent', 'review']) {
        await openAt(page, pageNamed(name));
        expect(await page.evaluate(() => window.innerWidth)).toBe(width);
        const back = await main(page)
          .getByRole('button', {name: 'Back', exact: true})
          .boundingBox();
        // Review's way on is Start run, as wide as its words: only its edge is the same.
        const next = await main(page)
          .getByRole('button', {name: name === 'review' ? 'Start run' : 'Continue', exact: true})
          .boundingBox();
        expect(back).not.toBeNull();
        expect(next).not.toBeNull();
        boxes.push({
          back,
          next: name === 'review' ? {y: next?.y, height: next?.height} : next,
        });
        // Pinned inside the window, below the content, even when the content scrolls.
        expect((next?.y ?? 0) + (next?.height ?? 0)).toBeLessThanOrEqual(height);
      }
      const [first, ...rest] = boxes;
      const review = rest.pop();
      for (const each of rest) expect(each).toEqual(first);
      expect(review?.back).toEqual(first?.back);
      const forward = first?.next as {y: number; height: number} | null;
      expect(review?.next).toEqual({y: forward?.y, height: forward?.height});
    });
  }
}

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

const WINDOWS = [
  {width: 1440, height: 1100},
  {width: 1000, height: 700},
] as const;

for (const {width, height} of WINDOWS) {
  test(`Back and Continue stay inside the ${width}x${height} window, in one place, whatever the content does`, async ({
    page,
  }) => {
    const newRun = SEED.newRun;
    if (newRun === undefined) throw new Error('the seed has no New run helper');
    await page.setViewportSize({width, height});
    const seed = {...SEED, newRun: {...newRun, examples: longCatalogue(40)}};
    const boxOf = async (name: string) =>
      main(page).getByRole('button', {name, exact: true}).boundingBox();
    const inside = (box: {x: number; y: number; width: number; height: number} | null) => {
      expect(box).not.toBeNull();
      expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
      expect(box?.y ?? -1).toBeGreaterThanOrEqual(0);
      expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(width);
      expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(height);
    };
    const backs: unknown[] = [];
    const nexts: unknown[] = [];
    for (const name of ['start', 'domain', 'model', 'review']) {
      await openAt(page, pageNamed(name), seed);
      // Geometry is meaningful after font swapping and the rendered layout have settled.
      await settled(page);
      expect(page.viewportSize()).toEqual({width, height});
      const forward = name === 'review' ? 'Start run' : 'Continue';
      const hasBack = name !== 'start';
      const backBox = async () => (hasBack ? boxOf('Back') : null);
      const before = {back: await backBox(), next: await boxOf(forward)};
      // The first step has no Back.
      if (hasBack) inside(before.back);
      inside(before.next);
      const region = main(page).locator('.vs-page-scroll');
      if (name === 'model') {
        // The long list outgrows the window, so it scrolls in its own region.
        expect(await region.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(
          true,
        );
      }
      await region.evaluate(element => element.scrollTo(0, element.scrollHeight));
      const after = {back: await backBox(), next: await boxOf(forward)};
      expect(after).toEqual(before);
      if (hasBack) backs.push(before.back);
      // Review's Start run is as wide as its words; the edge it sits on is the same.
      nexts.push({y: before.next?.y, height: before.next?.height});
    }
    for (const each of backs) expect(each).toEqual(backs[0]);
    for (const each of nexts) expect(each).toEqual(nexts[0]);
  });
}

test('step 1 shows the wordmark, small and decorative, above its question; step 2 does not', async ({
  page,
}) => {
  await openAt(page, pageNamed('start'));
  const art = main(page).locator('.vs-wordmark-art');
  await expect(art).toBeVisible();
  await expect(art).toHaveAttribute('aria-hidden', 'true');
  const box = await art.boundingBox();
  const heading = await question(page, 'What do you want to optimize?').boundingBox();
  expect(box?.height).toBeCloseTo(40, 0);
  expect((box?.y ?? 1e9) + (box?.height ?? 0)).toBeLessThanOrEqual(heading?.y ?? 0);
  // Nothing with it but the question's own text.
  await expect(main(page).locator('.vs-wordmark')).toHaveText(/^[\u2500-\u259f\s]+$/);
  await choose(page, 'Start from', 'An example');
  await proceed(page);
  await expect(question(page, 'Which domain?')).toBeVisible();
  await expect(main(page).locator('.vs-wordmark')).toHaveCount(0);
});

test('a model is one card however many kinds of hardware it has examples for, and hardware follows', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('model'), {
    ...SEED,
    newRun: {...newRun, examples: longCatalogue(0)},
  });
  const qwen = main(page).getByRole('radiogroup', {name: 'Qwen'}).getByRole('radio');
  await expect(qwen).toHaveCount(1);
  await expect(qwen).toHaveAccessibleName(/^Qwen3\.5 9B$/);
  await choose(page, 'Qwen', /Qwen3\.5 9B/);
  await proceed(page);
  await expect(main(page).getByRole('radio')).toHaveCount(2);
  await expect(main(page).getByRole('radiogroup', {name: 'AMD'})).toContainText('MI210');
  await expect(main(page).getByRole('radiogroup', {name: 'NVIDIA'})).toContainText('H100');
});

test('a step draws no explanatory sentence under its question, and a blocked Continue no caption', async ({
  page,
}) => {
  await openAt(page, pageNamed('start'));
  await expect(main(page)).not.toContainText('Start from one of our examples');
  await expect(main(page)).not.toContainText('A ready-made task');
  await expect(main(page)).not.toContainText('Your own repository');
  await expect(main(page)).not.toContainText('Answer the question above');
  await expect(main(page).getByRole('button', {name: 'Continue'})).toHaveAttribute(
    'aria-disabled',
    'true',
  );
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

for (const width of [1440, 1000, 660]) {
  test(`every step has its number in a neutral circle, the current one filled, no status colour, at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({width, height: 900});
    await openAt(page, pageNamed('goal LLM'));
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    const markers = await page.evaluate(() =>
      [...document.querySelectorAll('.vs-sidebar-marker')].map(marker => ({
        text: marker.textContent,
        state: marker.getAttribute('data-state'),
        font: getComputedStyle(marker).fontFamily,
        radius: getComputedStyle(marker).borderRadius,
        dots: marker.closest('.vs-sidebar-item')?.querySelectorAll('.vs-dot').length,
      })),
    );
    expect(markers.map(each => each.text)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8']);
    expect(markers.map(each => each.state)).toEqual([
      'neutral',
      'neutral',
      'neutral',
      'neutral',
      'current',
      'neutral',
      'neutral',
      'muted',
    ]);
    // The collapsed control (shown when narrow) counts the same steps.
    await expect(page.getByText('5 of 8 · Goal').first()).toBeAttached();
    expect(markers.every(each => each.font.includes('JetBrains Mono'))).toBe(true);
    expect(markers.every(each => each.dots === 0)).toBe(true);
  });
}

for (const width of [1440]) {
  test(`the longest step answer has room to spare on one line at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    await openAt(page, pageNamed('goal LLM'));
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    const slack = await page.evaluate(() =>
      [...document.querySelectorAll('.vs-sidebar-text > *')].map(line => {
        const range = document.createRange();
        range.selectNodeContents(line);
        const box = line.getBoundingClientRect();
        return {text: line.textContent, slack: box.width - range.getBoundingClientRect().width};
      }),
    );
    const longest = slack.find(each => each.text === 'Opus 4.5 \u00b7 Claude Code');
    expect(longest?.slack ?? 0).toBeGreaterThan(24);
  });
}

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
  await choose(page, 'Start from', 'An example');
  await proceed(page);
  await choose(page, 'Domain', 'LLM serving');
  await proceed(page);
  // Leaving New run and coming back finds it where it was left.
  await home();
  await expect(main(page)).toContainText('LLM serving');
  await expect(main(page)).toContainText('3 of 8 · Model');
  await expect(question(page, 'Which model do you want to serve?')).toHaveCount(0);
  await main(page).getByRole('button', {name: 'Open'}).click();
  await expect(question(page, 'Which model do you want to serve?')).toBeVisible();
  await page.reload();
  await home();
  await main(page).getByRole('button', {name: 'Open'}).click();
  await expect(question(page, 'Which model do you want to serve?')).toBeVisible();
  await expect(steps(page).getByRole('button', {name: /Domain/})).toContainText('LLM serving');
  await home();
  await main(page).getByRole('button', {name: 'Discard'}).click();
  await expect(main(page).getByText('Draft', {exact: true})).toHaveCount(0);
  await rail.getByRole('button', {name: 'New', exact: true}).click();
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
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
      features: [...REQUIRED_HELPER_FEATURES, 'run_roles'],
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
  await expect(main(page)).toContainText('Needs an update');
  await expect(main(page)).toContainText('.vibesys/tasks/vllm/OBJECTIVE.md');
  await expect(main(page)).not.toContainText('Goal not shown here');
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
  await expect(main(page)).toContainText('No examples');
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
  await expect(main(page)).toContainText('VibeSys on host-b');
  await expect(main(page)).toContainText('Needs an update');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await start(page).click({force: true});
  const {started, preflights} = await fakeNewRun(page);
  expect(started).toEqual([]);
  expect(preflights).toEqual([]);
});

function withoutRunTrace() {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  return {
    ...SEED,
    newRun: {
      ...newRun,
      features: REQUIRED_HELPER_FEATURES.filter(feature => feature !== 'run_trace'),
    },
  };
}

test('a machine that serves everything but the run trace says Needs an update on Where', async ({
  page,
}) => {
  await openAt(page, pageNamed('where'), withoutRunTrace());
  await expect(main(page).getByText('Needs an update', {exact: true})).toBeVisible();
  await expect(
    main(page).locator('[aria-description="VibeSys on host-b needs an update"]'),
  ).toBeAttached();
});

test('a machine that serves everything but the run trace says Needs an update on Review and never starts', async ({
  page,
}) => {
  await openAt(page, pageNamed('review project'), withoutRunTrace());
  await expect(main(page)).toContainText('VibeSys on host-b');
  await expect(main(page)).toContainText('Needs an update');
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await start(page).click({force: true});
  const {started} = await fakeNewRun(page);
  expect(started).toEqual([]);
});

test('a start whose reply was lost says on the page that it may have started, and Start waits until Runs was opened', async ({
  page,
}) => {
  await openAt(page, pageNamed('review project'));
  // The host takes the run and the reply is lost: the call rejects with nothing reported.
  await page.evaluate(() => {
    const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
    bridge['startRun'] = () => Promise.reject(new Error('the reply was lost'));
  });
  await start(page).click();
  // Without hovering: the words are on the page, Start is closed with its reason beside it, and
  // there is a way to look.
  await expect(main(page).getByText('May have started', {exact: true})).toBeVisible();
  await expect(start(page)).toHaveAttribute('aria-disabled', 'true');
  await expect(main(page).getByText('Check Runs first', {exact: true})).toBeVisible();
  await start(page).click({force: true});
  expect((await fakeNewRun(page)).started).toEqual([]);

  await main(page).getByRole('button', {name: 'Open Runs', exact: true}).click();
  await expect(sidebar(page).getByRole('button', {name: 'Runs'})).toHaveAttribute(
    'aria-current',
    'page',
  );
  // Back at the form: the person has looked, so Start is open again.
  await sidebar(page).getByRole('button', {name: 'New', exact: true}).click();
  await expect(main(page).getByText('May have started', {exact: true})).toHaveCount(0);
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
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
  await main(page).getByLabel('Reviewer reasoning effort').selectOption('high');
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
    newRun: {...newRun, features: REQUIRED_HELPER_FEATURES},
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
  await expect(toggle).toContainText('Same as the run');
  await toggle.click();
  const judge = main(page).getByLabel('Reviewer model');
  await expect(judge).toHaveValue('');
  // Models sit under their maker and family; the run's own harness is the only one a role may use.
  await expect(judge.locator('optgroup')).toHaveAttribute('label', 'Anthropic \u00b7 Claude');
  await expect(main(page).getByLabel('Reviewer harness')).toHaveCount(0);
  await judge.selectOption({label: 'Sonnet 4.5'});
  await expect(judge).not.toHaveValue('');
  // The run's own model is the same as no choice: the role follows the run again.
  await judge.selectOption({label: 'Opus 4.5'});
  await expect(judge).toHaveValue('');
  await judge.selectOption({label: 'Sonnet 4.5'});
  await main(page).getByRole('button', {name: 'Continue'}).click();
  await expect(main(page)).toContainText('Reviewer agent');
  await expect(main(page)).toContainText('Sonnet 4.5 \u00b7 Claude Code');
  await main(page)
    .getByText('Reviewer agent', {exact: true})
    .locator('xpath=ancestor::*[.//button][1]')
    .getByRole('button', {name: 'Change'})
    .click();
  await expect(main(page).getByLabel('Reviewer model')).toBeVisible();
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

test('a machine whose VibeSys cannot run an agent per role says Needs an update on that row, never beside an installed harness', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('agent project'), {
    ...SEED,
    newRun: {...newRun, features: [...REQUIRED_HELPER_FEATURES, 'run_roles']},
  });
  // One status for the row that cannot be used; the harness card keeps saying it is installed.
  const row = main(page).getByText('Use a different agent for a role').locator('..');
  await expect(row).toContainText('Needs an update');
  await expect(main(page).getByRole('radiogroup', {name: 'Harness'})).toContainText(
    'Installed on host-b',
  );
  await expect(main(page).getByText('Not supported on this machine')).toHaveCount(0);
  await expect(main(page).getByRole('button', {name: /different agent for a role/})).toHaveCount(0);
});

test('Where: a machine with nothing set up links to its Settings, and Back returns to the same step', async ({
  page,
}) => {
  await openAt(page, pageNamed('where'));
  const measured = main(page).getByRole('radiogroup', {name: 'Measured on'});
  await expect(measured.getByRole('radio')).toHaveCount(1);
  await expect(measured.getByRole('radio', {name: /Same machine/})).toBeVisible();
  await main(page).getByRole('button', {name: 'Set up on host-b'}).click();
  await expect(page.getByRole('combobox', {name: 'Machine'})).toHaveValue('ssh:host-b');
  await expect(
    main(page).getByRole('heading', {name: 'Clusters and clouds'}).first(),
  ).toBeVisible();
  await page.getByRole('button', {name: 'Back', exact: true}).click();
  await expect(question(page, /Where does the agent work/)).toBeVisible();
  await expect(steps(page).getByRole('button', {name: /Where it runs/})).toHaveAttribute(
    'aria-current',
    'page',
  );
  await expect(
    main(page)
      .getByRole('radiogroup', {name: 'Agent works on'})
      .getByRole('radio', {name: /host-b/}),
  ).toBeChecked();
});

test('Where: only what the machine has set up is offered, and the choice is the measurement target', async ({
  page,
}) => {
  await openAt(page, pageNamed('where'));
  await main(page)
    .getByRole('radiogroup', {name: 'Agent works on'})
    .getByRole('radio', {name: /This Mac/})
    .check();
  const measured = main(page).getByRole('radiogroup', {name: 'Measured on'});
  await expect(measured.getByRole('radio')).toHaveCount(5);
  for (const name of [
    'Same machine',
    'Slurm · lab-h100',
    'Slurm · campus',
    'Slurm · cluster-m',
    'Modal',
  ]) {
    await expect(measured.getByRole('radio', {name: new RegExp(name)})).toBeVisible();
  }
  await expect(main(page).getByRole('button', {name: /^Set up on/})).toHaveCount(0);
  await measured.getByRole('radio', {name: 'Slurm · lab-h100'}).check();
  await proceed(page);
  await proceed(page);
  await expect(main(page)).toContainText('Slurm · lab-h100');
  await main(page).getByRole('button', {name: 'Create the project'}).click();
  await expect(start(page)).not.toHaveAttribute('aria-disabled', 'true');
  await start(page).click();
  await expect(page.getByRole('radiogroup', {name: 'Show'})).toBeVisible();
  const {started} = await fakeNewRun(page);
  expect(started).toMatchObject([{measure_on: {kind: 'slurm', cluster: 'lab-h100'}}]);
});

test('Where: a GPU task the agent machine lacks is a misfit only when results are measured there', async ({
  page,
}) => {
  const where = pageNamed('where');
  await openAt(page, {
    ...where,
    answers: {
      ...where.answers,
      model: 'Olmo Hybrid 7B',
      hardware: 'nvidia|NVIDIA L4|1',
      variant: 'from-scratch',
    },
    machine: 'local',
  });
  const measured = main(page).getByRole('radiogroup', {name: 'Measured on'});
  const misfit = main(page).getByText('Hardware mismatch', {exact: true});
  await expect(misfit).toBeVisible();
  // The detail is the status word's description, not on the page.
  await expect(main(page).getByText(/The task targets/)).toHaveCount(0);
  await expect(misfit).toHaveAccessibleDescription(/^The task targets .*, but .* has /);
  for (const name of ['Slurm · lab-h100', 'Slurm · campus', 'Modal']) {
    await measured.getByRole('radio', {name: new RegExp(name)}).check();
    await expect(misfit).toHaveCount(0);
    await expect(main(page).getByText('Hardware fits', {exact: true})).toHaveCount(0);
  }
  await measured.getByRole('radio', {name: /Same machine/}).check();
  await expect(misfit).toBeVisible();
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

  const failed = page.getByText('Not read', {exact: true});
  await expect(failed).toBeVisible();
  await expect(failed).toHaveAccessibleDescription(/^Could not read the agents host-b runs\./);
  await expect(failed).toHaveAccessibleDescription(/Something unexpected happened\./);
  await expect(failed).not.toHaveAccessibleDescription(/is not a function/);
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

test('a machine that cannot list its examples says Failed and lets another machine be chosen', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  await openAt(page, pageNamed('example'), {
    ...SEED,
    newRun: {...newRun, examplesFail: ['ssh:host-b']},
  });
  await expect(main(page).getByText('Failed', {exact: true})).toBeVisible();
  await expect(main(page).getByText('Listing', {exact: true})).toHaveCount(0);
  // The failed page is replaced by the new machine's listing as the choice lands.
  await main(page)
    .getByRole('radiogroup', {name: 'Machine', exact: true})
    .getByRole('radio', {name: /This Mac/})
    .click({force: true});
  await expect(question(page, 'Which domain?')).toBeVisible();
  await expect(main(page).getByRole('radiogroup', {name: 'Domain'})).toBeVisible();
  await expect(main(page).getByText('Failed', {exact: true})).toHaveCount(0);
});

test('a fresh profile whose This Mac cannot list examples offers the other machines at once', async ({
  page,
}) => {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  // A fresh profile has no recent run, so the form starts on This Mac.
  await open(page, {
    ...SEED,
    recent: [],
    listings: [],
    newRun: {...newRun, examplesFail: ['local']},
  });
  await expect(question(page, 'What do you want to optimize?')).toBeVisible();
  const start = main(page).getByRole('radiogroup', {name: 'Start from', exact: true});
  await expect(start).toContainText('Failed');
  // The first page itself offers the configured machines, the broken one chosen.
  const machines = main(page).getByRole('radiogroup', {name: 'Machine', exact: true});
  await expect(machines.getByRole('radio')).toHaveCount(2);
  await expect(machines.getByRole('radio', {name: /This Mac/})).toBeChecked();
  await machines.getByRole('radio', {name: /host-b/}).click({force: true});
  // Its examples are listed, so the example path opens.
  await expect(start).not.toContainText('Failed');
  await expect(start).toContainText('examples');
  await expect(machines).toHaveCount(0);
});
