import AxeBuilder from '@axe-core/playwright';
import {expect, type Page, test} from '@playwright/test';
import type {ExampleList} from '@vibesys/contracts/host-helper';
import {exampleCatalogue, storeRunFixture} from '@vibesys/run-fixtures';
import {
  installFakeBridge,
  installFakeChrome,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from '../src/testing/fake-bridge.js';
import {settled} from './checks/settle.js';
import {installGateway, wire} from './fake-gateway.js';
import {installFakeRun} from './fake-run.js';
import {addInitScripts, initScript} from './init-scripts.js';
import {choose, main, NEW_RUN_PAGES, openAt, proceed, question} from './new-run-flow.js';
import {type FakeHostWindow, openNewRun as open, PROJECT, SEED} from './world.js';

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

test('every step number is drawn in the mono face, at every width', async ({page}) => {
  await openAt(page, pageNamed('goal LLM'));
  for (const width of [1440, 1000, 660]) {
    await page.setViewportSize({width, height: 900});
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    const fonts = await page.evaluate(() =>
      [...document.querySelectorAll('.vs-sidebar-marker')].map(
        marker => getComputedStyle(marker).fontFamily,
      ),
    );
    expect(fonts).toHaveLength(8);
    expect(fonts.every(font => font.includes('JetBrains Mono'))).toBe(true);
  }
});

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
    initScript(installFakeChrome, seed),
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
