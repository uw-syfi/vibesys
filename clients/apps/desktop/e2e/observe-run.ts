/**
 * The UI half of the desktop path end to end test (`gateway/e2e/desktop_path_test.go`).
 *
 * That test starts the real daemon and a scripted world (a lead, two workers and a judge on a toy
 * task, no model), runs it to the lead's final report, which ends the run by itself, then runs this with `VIBESYS_E2E_WORLD` naming
 * what the daemon reported. This launches the built Electron app with its HOME set to the
 * daemon's, so the app's local gateway is the running one, and checks what a person sees, by
 * role and accessible name: the run listed in the history as finished, with no Stop, its report with
 * the lead's summary, the harness on every agent row, the lead's report. Opening Catch me up makes no
 * check-in; New check-in makes one and shows the window's figures and the report (the lead has
 * finished, so the host does not ask it for another). The test then checks that no process of the
 * app is left.
 *
 * Every wait is an expectation with a bound that only guards against a hang; nothing sleeps.
 */
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {
  type ElectronApplication,
  _electron as electron,
  expect,
  type Page,
  test,
} from '@playwright/test';

/** An agent of the run as the daemon reported it, by the daemon's own enum names. */
interface WorldAgent {
  readonly id: string;
  readonly role: string;
  readonly harness: string;
}

/** What the conductor knows about the run, from `gateway/e2e/desktop_ui_test.go` (`uiSpecWorld`). */
interface World {
  readonly home: string;
  readonly runId: string;
  readonly task: string;
  readonly report: string;
  readonly agents: readonly WorldAgent[];
}

/** The words the agent list shows for each role and harness the scripted world uses. */
const ROLE_WORD: Readonly<Record<string, string>> = {
  AGENT_ROLE_LEAD: 'Lead',
  AGENT_ROLE_WORKER: 'Worker',
  AGENT_ROLE_JUDGE: 'Reviewer',
};
const HARNESS_WORD: Readonly<Record<string, string>> = {
  HARNESS_CLAUDE_CODE: 'Claude Code',
  HARNESS_CODEX: 'Codex',
};

const WORLD_KEYS = ['home', 'runId', 'task', 'report', 'agents'];

/** Text as the app shows it: each full commit id (40 hex digits) is cut to its first 8 digits. */
const shown = (text: string): string =>
  text.replace(/\b[0-9a-f]{40}\b/g, commit => commit.slice(0, 8));

function readWorld(env: NodeJS.ProcessEnv = process.env): World {
  const raw = env['VIBESYS_E2E_WORLD'];
  if (raw === undefined || raw === '') {
    throw new Error(
      'VIBESYS_E2E_WORLD is not set: this test is run by gateway/e2e with VIBESYS_E2E_DESKTOP_UI=1, which starts the world it attaches to',
    );
  }
  const parsed: unknown = JSON.parse(raw);
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('VIBESYS_E2E_WORLD must be a JSON object');
  }
  const unknown = Object.keys(parsed).filter(key => !WORLD_KEYS.includes(key));
  if (unknown.length > 0)
    throw new Error(`VIBESYS_E2E_WORLD has unknown keys: ${unknown.join(', ')}`);
  const world = parsed as Partial<Record<string, unknown>>;
  for (const key of ['home', 'runId', 'task', 'report'] as const) {
    if (typeof world[key] !== 'string' || world[key] === '') {
      throw new Error(`VIBESYS_E2E_WORLD.${key} must be a non-empty string`);
    }
  }
  if (!Array.isArray(world['agents']) || world['agents'].length === 0) {
    throw new Error('VIBESYS_E2E_WORLD.agents must list the run’s agents');
  }
  for (const agent of world['agents'] as WorldAgent[]) {
    if (ROLE_WORD[agent.role] === undefined || HARNESS_WORD[agent.harness] === undefined) {
      throw new Error(
        `the world has an agent this test has no words for: ${JSON.stringify(agent)}`,
      );
    }
  }
  return world as unknown as World;
}

/**
 * The arguments Electron needs on Linux in CI: Chromium's SUID sandbox helper cannot have the
 * ownership it requires under root or on a hosted runner, so CI opts out for this child process
 * only. The same policy as the other Electron suites; no production window is changed by it.
 */
function runtimeArgs(): string[] {
  const opted = process.env['VIBESYS_ELECTRON_E2E_DISABLE_CHROMIUM_SANDBOX'] === '1';
  return process.platform === 'linux' && (process.getuid?.() === 0 || opted)
    ? ['--no-sandbox']
    : [];
}

/** How many of the run's agents have this role and harness, by the daemon's report. */
function expectedAgents(world: World, role: string, harness: string): number {
  return world.agents.filter(agent => agent.role === role && agent.harness === harness).length;
}

/** Where the page's title was on each destination the walk visited: the frame keeps it in one place. */
const titles: {readonly name: string; readonly left: number; readonly top: number}[] = [];

/**
 * Record where the title is on the destination now on screen, and, with `VIBESYS_E2E_SHOTS` set to a
 * folder, capture the app's window (the window alone, never the screen) there.
 */
async function seen(page: Page, name: string): Promise<void> {
  const heading = page.getByRole('heading', {level: 1});
  await expect(heading).toHaveCount(1);
  await expect(heading).toBeVisible();
  // A page eases in: read the title once the finite animations are over (a looping one, such as a
  // live bar's pulse, never ends and moves nothing).
  // (A string, because this file is compiled without the DOM's types: it runs in the window.)
  await page.evaluate(
    'Promise.all(document.getAnimations().filter(each => each.effect?.getComputedTiming().iterations !== Infinity).map(each => each.finished.catch(() => undefined)))',
  );
  const box = await heading.boundingBox();
  expect(box, `the title of ${name} is drawn`).not.toBeNull();
  titles.push({
    name,
    left: Math.round(box?.x ?? Number.NaN),
    top: Math.round(box?.y ?? Number.NaN),
  });
  const folder = process.env['VIBESYS_E2E_SHOTS'];
  if (folder !== undefined && folder !== '')
    await page.screenshot({path: join(folder, `${name}.png`)});
}

async function launch(world: World, userData: string): Promise<ElectronApplication> {
  return electron.launch({
    args: [fileURLToPath(new URL('..', import.meta.url)), ...runtimeArgs()],
    // HOME is where the app looks for the gateway (`$HOME/.vibesys`): the daemon's own. The rest
    // of the environment, including the mark the conductor finds leftovers by, is inherited.
    env: {...process.env, HOME: world.home, VIBESYS_DESKTOP_USER_DATA: userData},
  });
}

/**
 * New run opens as a page with its two entry cards and the machine strip, and Escape puts it away, back
 * to the run list. Which examples fit is the host's catalogue, which this world does not script, so the
 * Model control and the run's two bounds under a picked example are covered by the DOM tests of the
 * page, which give it a catalogue. With a window folder set, the page is captured (the window alone).
 */
async function checkTheNewRunPageOpensAndCloses(page: Page): Promise<void> {
  await page.getByRole('button', {name: 'New run'}).click();
  await expect(page.getByRole('heading', {level: 1, name: 'New run'})).toBeVisible();
  await expect(page.getByRole('button', {name: /Start from an example/})).toBeVisible();
  await expect(page.getByRole('button', {name: /Bring a repository/})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Edit in Machines'})).toBeVisible();
  const folder = process.env['VIBESYS_E2E_SHOTS'];
  if (folder !== undefined && folder !== '')
    await page.screenshot({path: join(folder, '1b-start.png')});
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', {name: 'New run'})).toBeVisible();
}

/** The name `seen` records the run report's title under. */
const REPORT_SHOT = '2-report';

async function checkRunIsListedAndRunning(page: Page, world: World): Promise<void> {
  // The lead's final report ended the run, so it is in the Ended section of the run list; each run
  // is a card named by its run (the task), whose status chip says the rest and whose one primary
  // action opens it.
  const ended = page.getByRole('region', {name: 'Ended runs'});
  const card = ended.getByRole('article', {name: new RegExp(`^${world.task}\\b`)});
  await expect(card).toBeVisible();
  await expect(card).toContainText('Finished');
  await seen(page, '1-runs');
  await checkTheNewRunPageOpensAndCloses(page);
  await card.getByRole('button', {name: `Open run ${world.task}`}).click();
  await expect(page.getByRole('navigation', {name: 'Where you are'})).toContainText(world.task);
  await seen(page, REPORT_SHOT);
  // Stop is offered only while a run runs.
  await expect(page.getByRole('button', {name: 'Stop run'})).toHaveCount(0);
}

/**
 * The report's ideas and workstreams: both sections are on the page, and a chart point, reached and
 * chosen by the keyboard alone, says what it is: the idea its job measured or that it names no plan.
 * With a window folder set, the ideas section is captured (the window alone).
 */
async function checkTheIdeasAndTheChartLink(page: Page): Promise<void> {
  const ideas = page.getByRole('region', {name: 'Ideas', exact: true});
  const workstreams = page.getByRole('region', {name: 'Workstreams', exact: true});
  await ideas.scrollIntoViewIfNeeded();
  await expect(ideas).toBeVisible();
  await expect(workstreams).toBeVisible();
  const folder = process.env['VIBESYS_E2E_SHOTS'];
  const points = page.locator('.vs-chart-pick');
  if ((await points.count()) > 0) {
    // The chart's points are one tab stop; Enter chooses the focused one.
    const first = points.first();
    await first.focus();
    await page.keyboard.press('Enter');
    const chosen = page.getByRole('status', {name: 'Chosen point'});
    await expect(chosen).toContainText(/No plan|Idea #|#\d+/);
  }
  if (folder !== undefined && folder !== '')
    await page.screenshot({path: join(folder, '2b-ideas.png')});
  await page.getByRole('heading', {level: 1}).scrollIntoViewIfNeeded();
}

/** A run opens on its report, which carries the lead's latest report as the run's summary. */
async function checkTheReportShowsTheLeadsSummary(page: Page, world: World): Promise<void> {
  await expect(page.getByRole('region', {name: 'Lead summary'})).toContainText(shown(world.report));
  // The report shows no agent's turns and no list of agents: the agents have a page of their own,
  // one press away.
  await expect(page.getByRole('tree')).toHaveCount(0);
  await expect(page.getByRole('heading', {level: 1})).toContainText(world.task);
  // The cost is a page of its own, and Escape returns to the link that opened it.
  await page.getByRole('button', {name: 'Open cost'}).click();
  await expect(page.getByRole('heading', {level: 1})).toHaveText('Cost');
  await seen(page, '3-cost');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('heading', {level: 1})).toContainText(world.task);
  await expect(page.getByRole('button', {name: 'Open cost'})).toBeFocused();
  await page.getByRole('button', {name: 'Open agents'}).click();
  await expect(page.getByRole('heading', {level: 1})).toHaveText('Agents');
  await seen(page, '4-agents');
}

/**
 * The Agents page lists every agent of the run as a row; each opens its own page, whose title is
 * the agent's name and whose header says its harness. The rows are counted by role and harness
 * against the daemon's report, so a missing, a wrong and an extra label fail, and Escape from an
 * agent's page returns to the Agents page with the focus on the row that was opened.
 */
async function checkEveryAgentShowsItsHarness(page: Page, world: World): Promise<void> {
  const rows = page.getByRole('listbox', {name: 'Agents over time'}).getByRole('option');
  await expect(rows).toHaveCount(world.agents.length);
  const title = page.getByRole('heading', {level: 1});
  const counted = new Map<string, number>();
  for (let at = 0; at < world.agents.length; at += 1) {
    await rows.nth(at).click();
    await expect(title).not.toHaveText('Agents');
    if (at === 0) await seen(page, '5-agent');
    const name = (await title.textContent()) ?? '';
    // An agent is named by its capitalised role and number (`Worker 3`, `Lead`), as every screen says it.
    const role = Object.entries(ROLE_WORD).find(([, word]) =>
      name.toLowerCase().startsWith(word.toLowerCase()),
    );
    expect(role, `the page of ${name} names a role`).toBeDefined();
    let harness: string | undefined;
    for (const [id, word] of Object.entries(HARNESS_WORD)) {
      const chip = page.locator('.vs-page-header').getByText(word, {exact: true});
      if ((await chip.count()) > 0) harness = id;
    }
    expect(harness, `the page of ${name} says its harness`).toBeDefined();
    const key = `${role?.[0]}|${harness}`;
    counted.set(key, (counted.get(key) ?? 0) + 1);
    await page.keyboard.press('Escape');
    await expect(title).toHaveText('Agents');
    await expect(rows.nth(at)).toBeFocused();
  }
  for (const role of Object.keys(ROLE_WORD)) {
    for (const harness of Object.keys(HARNESS_WORD)) {
      expect(
        counted.get(`${role}|${harness}`) ?? 0,
        `${ROLE_WORD[role]} agents on ${harness}`,
      ).toBe(expectedAgents(world, role, harness));
    }
  }
}

/** The lead's report is the run's summary on its report page, which the run's name in the trail leads back to. */
async function checkTheLeadsReportIsShown(page: Page, world: World): Promise<void> {
  await page
    .getByRole('navigation', {name: 'Where you are'})
    .getByRole('button', {name: world.task})
    .click();
  await expect(page.getByRole('region', {name: 'Lead summary'})).toContainText(shown(world.report));
}

/** The check-ins listed on the catch-up page, newest first. */
const checkIns = (page: Page) => page.getByRole('listbox', {name: 'Check-ins'}).getByRole('option');

/**
 * Open Catch me up on the run, then press New check-in. Its lead has given its final report, so the host cannot ask
 * it for another: the window's facts are the daemon's own count of the run's agents, and the
 * report is the one posted inside the window. A second press makes a second window, which is empty.
 */
async function pressCatchUpAndSeeWhatChanged(page: Page, world: World): Promise<void> {
  // Opening the page only reads: it makes no check-in, and opening it again makes none.
  await page.getByRole('button', {name: 'Catch me up'}).click();
  await expect(checkIns(page)).toHaveCount(0);
  await page
    .getByRole('navigation', {name: 'Where you are'})
    .getByRole('button', {name: world.task})
    .click();
  await page.getByRole('button', {name: 'Catch me up'}).click();
  await expect(checkIns(page)).toHaveCount(0);
  await page.getByRole('button', {name: 'New check-in'}).click();
  await expect(checkIns(page)).toHaveCount(1);
  await seen(page, '6-catch-up');
  await expect(page.getByRole('region', {name: 'Agents', exact: true})).toContainText(
    new RegExp(`Started\\s*${world.agents.length}(?!\\d)`),
  );
  await expect(page.getByRole('region', {name: 'Lead summary'})).toContainText(shown(world.report));
  await expect(page.getByText('Writing…')).toHaveCount(0);
  await page.getByRole('button', {name: 'New check-in'}).click();
  await expect(checkIns(page)).toHaveCount(2);
  // The run is where it was left: its page is one step back.
  await page
    .getByRole('navigation', {name: 'Where you are'})
    .getByRole('button', {name: world.task})
    .click();
  await expect(page.getByRole('button', {name: 'Stop run'})).toHaveCount(0);
}

/** Settings is the other destination of the rail; its title is where Runs' is. */
async function checkSettingsTitleIsInTheSamePlace(page: Page): Promise<void> {
  await page.getByRole('button', {name: 'Settings'}).click();
  await expect(page.getByRole('heading', {level: 1})).toHaveText('Settings');
  await seen(page, '7-settings');
  // The titles of destinations without a trail (Runs, Settings) are at one height, and the titles
  // under a trail at another. They share one left edge except for the run report: its dense grid
  // fills the window, so its title keeps the page gutter while the other pages are centred. The
  // exception is recorded under law 1 of the visual spec.
  const bare = new Set(['1-runs', '7-settings']);
  const placesOf = (wanted: boolean) =>
    new Set(titles.filter(each => bare.has(each.name) === wanted).map(each => each.top));
  expect(placesOf(true).size, JSON.stringify(titles)).toBe(1);
  expect(placesOf(false).size, JSON.stringify(titles)).toBe(1);
  const left = new Set(titles.filter(each => each.name !== REPORT_SHOT).map(each => each.left));
  expect(left.size, JSON.stringify(titles)).toBe(1);
}

/**
 * How long the app may take to quit on its own after the checks. It only guards against a hang
 * (the app does not exit when asked to quit while it is connected to a gateway, which is tracked
 * as issue 3854); nothing waits for it when the app quits.
 */
const QUIT_BOUND_MS = 10_000;

/**
 * Ask the app to quit and wait for its process to end; if it does not within the bound, kill it, so
 * that no process of the app is left either way. The conductor then looks for any that remain.
 */
async function quit(app: ElectronApplication): Promise<void> {
  const process = app.process();
  const exited = new Promise<void>(resolve => {
    if (process.exitCode !== null || process.signalCode !== null) resolve();
    else process.once('exit', () => resolve());
  });
  void app.close().catch(() => {});
  let guard: NodeJS.Timeout | undefined;
  const hung = new Promise<'hung'>(resolve => {
    guard = setTimeout(() => resolve('hung'), QUIT_BOUND_MS);
  });
  const outcome = await Promise.race([exited.then(() => 'exited' as const), hung]);
  clearTimeout(guard);
  if (outcome === 'hung') {
    test.info().annotations.push({
      type: 'app-did-not-quit',
      description: `the app was still running ${QUIT_BOUND_MS} ms after it was asked to quit; it was killed`,
    });
    process.kill('SIGKILL');
    await exited;
  }
}

test('the app lists the scripted run, shows each agent’s harness and the report, catches the viewer up on a run its final report ended', async () => {
  const world = readWorld();
  const userData = await mkdtemp(join(tmpdir(), 'vsd-ui-'));
  let app: ElectronApplication | undefined;
  let page: Page | undefined;
  try {
    app = await launch(world, userData);
    page = await app.firstWindow();
    await checkRunIsListedAndRunning(page, world);
    await checkTheIdeasAndTheChartLink(page);
    await pressCatchUpAndSeeWhatChanged(page, world);
    await checkTheReportShowsTheLeadsSummary(page, world);
    await checkEveryAgentShowsItsHarness(page, world);
    await checkTheLeadsReportIsShown(page, world);
    await checkSettingsTitleIsInTheSamePlace(page);
  } catch (error) {
    // What the screen showed, by role and name, says more than the failed expectation does.
    const shown = await page
      ?.locator('body')
      .ariaSnapshot({timeout: 5_000})
      .catch(() => undefined);
    if (shown !== undefined) console.error(`The window at the failure:\n${shown}`);
    throw error;
  } finally {
    if (app !== undefined) await quit(app);
    await rm(userData, {recursive: true, force: true});
  }
});
