/**
 * The UI half of the desktop path end to end test (`gateway/e2e/desktop_path_test.go`).
 *
 * That test starts the real daemon and a scripted world (a lead, two workers and a judge on a toy
 * task, no model), runs it to the lead's report, then runs this with `VIBESYS_E2E_WORLD` naming
 * what the daemon reported. This launches the built Electron app with its HOME set to the
 * daemon's, so the app's local gateway is the running one, and checks what a person sees, by
 * role and accessible name: the run listed and running, the harness on every agent row, the
 * lead's report. It presses Catch me up twice and sees the window's figures and the report (the
 * lead has finished, so the host does not ask it for another), presses Stop and sees the run end,
 * and catches up once more on the run that is over. The test then checks the daemon's side of
 * Stop and that no process of the app is left.
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

async function launch(world: World, userData: string): Promise<ElectronApplication> {
  return electron.launch({
    args: [fileURLToPath(new URL('..', import.meta.url)), ...runtimeArgs()],
    // HOME is where the app looks for the gateway (`$HOME/.vibesys`): the daemon's own. The rest
    // of the environment, including the mark the conductor finds leftovers by, is inherited.
    env: {...process.env, HOME: world.home, VIBESYS_DESKTOP_USER_DATA: userData},
  });
}

async function checkRunIsListedAndRunning(page: Page, world: World): Promise<void> {
  // A running run is in the list of live runs; a row is named by its run (the task) and says the
  // rest, its state first, in its description.
  const live = page.getByRole('listbox', {name: 'Live runs'});
  const row = live.getByRole('option', {name: new RegExp(`^${world.task}\\b`)});
  await expect(row).toBeVisible();
  await expect(row).toContainText('Running');
  await row.click();
  await expect(page.getByRole('navigation', {name: 'Where you are'})).toContainText(world.task);
  // Stop is offered only while a run runs.
  await expect(page.getByRole('button', {name: 'Stop run'})).toBeVisible();
}

async function checkEveryAgentShowsItsHarness(page: Page, world: World): Promise<void> {
  const tree = page.getByRole('tree', {name: 'Agents in this run'});
  await expect(tree.getByRole('treeitem')).toHaveCount(world.agents.length);
  // A row reads: its state, the agent's name ("Worker 2"), its harness. Each role is counted
  // by each harness against the daemon's report, so a missing, a wrong and an extra label fail.
  for (const [role, roleWord] of Object.entries(ROLE_WORD)) {
    for (const [harness, harnessWord] of Object.entries(HARNESS_WORD)) {
      const rows = tree.getByRole('treeitem', {
        name: new RegExp(`\\b${roleWord}\\b.*\\b${harnessWord}$`),
      });
      await expect(rows, `${roleWord} rows on ${harnessWord}`).toHaveCount(
        expectedAgents(world, role, harness),
      );
    }
  }
}

async function checkTheLeadsReportIsShown(page: Page, world: World): Promise<void> {
  await page.getByRole('tab', {name: 'Handoffs and reports'}).click();
  await expect(page.getByRole('region', {name: 'Final report'})).toContainText(world.report);
}

/** The check-ins listed on the catch-up page, newest first. */
const checkIns = (page: Page) =>
  page.getByRole('listbox', {name: 'Check-ins'}).getByRole('option');

/**
 * Press Catch me up on the live run. Its lead has given its final report, so the host cannot ask
 * it for another: the window's facts are the daemon's own count of the run's agents, and the
 * report is the one posted inside the window. A second press makes a second window, which is empty.
 */
async function pressCatchUpAndSeeWhatChanged(page: Page, world: World): Promise<void> {
  await page.getByRole('button', {name: 'Catch me up'}).click();
  await expect(checkIns(page)).toHaveCount(1);
  await expect(page.getByRole('region', {name: 'Agents', exact: true})).toContainText(
    new RegExp(`Started\\s*${world.agents.length}(?!\\d)`),
  );
  await expect(page.getByRole('region', {name: 'Lead summary'})).toContainText(world.report);
  await expect(page.getByText('Writing…')).toHaveCount(0);
  await page.getByRole('button', {name: 'Catch me up'}).click();
  await expect(checkIns(page)).toHaveCount(2);
  // The run is where it was left: its page is one step back.
  await page
    .getByRole('navigation', {name: 'Where you are'})
    .getByRole('button', {name: world.task})
    .click();
  await expect(page.getByRole('button', {name: 'Stop run'})).toBeVisible();
}

/** A run that is over can be caught up on too: a third check-in, with no lead to write for it. */
async function pressCatchUpOnTheEndedRun(page: Page): Promise<void> {
  await page.getByRole('button', {name: 'Catch me up'}).click();
  await expect(checkIns(page)).toHaveCount(3);
  await expect(page.getByText('Writing…')).toHaveCount(0);
}

async function pressStopAndSeeTheRunEnd(page: Page): Promise<void> {
  await page.getByRole('button', {name: 'Stop run'}).click();
  await expect(page.getByRole('status', {name: 'Stop: outcome'})).toContainText('accepted');
  // The run is over when it reads Finished and offers no Stop.
  await expect(page.getByText('Finished', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Stop run'})).toHaveCount(0);
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

test('the app lists the scripted run, shows each agent’s harness and the report, catches the viewer up, and Stop ends the run', async () => {
  const world = readWorld();
  const userData = await mkdtemp(join(tmpdir(), 'vsd-ui-'));
  let app: ElectronApplication | undefined;
  let page: Page | undefined;
  try {
    app = await launch(world, userData);
    page = await app.firstWindow();
    await checkRunIsListedAndRunning(page, world);
    await checkEveryAgentShowsItsHarness(page, world);
    await checkTheLeadsReportIsShown(page, world);
    await pressCatchUpAndSeeWhatChanged(page, world);
    await pressStopAndSeeTheRunEnd(page);
    await pressCatchUpOnTheEndedRun(page);
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
