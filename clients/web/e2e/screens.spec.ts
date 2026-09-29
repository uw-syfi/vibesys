/**
 * Screenshots of the run window and every setup screen for review: CAPTURE_DIR=<dir> pnpm
 * --filter @vibesys/web exec playwright test screens. Each screen is written at 1440 and 1024,
 * dark and light, as <name>-<width>-<theme>.png (setup screens: setup-<name>-<width>-<theme>.png).
 * Without CAPTURE_DIR the spec is skipped (CI runs app.spec and setup.spec only).
 */
import {join} from 'node:path';
import {type Page, test} from '@playwright/test';
import {runHref} from '../src/route.js';
import {FINISHED, type Gateway, mockGateway, ROUND_6_FINISHED} from './gateway.js';
import {
  AUTH,
  FINISHED_RUN,
  GATEWAY_WS,
  HOME,
  type HomeOptions,
  mockHome,
  PROJECT_ID,
  ROOT,
} from './home.js';

const OUT = process.env['CAPTURE_DIR'];

interface Screen {
  name: string;
  through?: number;
  act?: (page: Page, gateway: Gateway) => Promise<void>;
}

const runs = (page: Page) => page.getByRole('navigation', {name: 'Runs'});

const SCREENS: Screen[] = [
  {name: 'live'},
  {name: 'finished', through: FINISHED},
  {
    name: 'round3',
    act: async page => {
      await runs(page)
        .getByRole('button', {name: /^Round 3,/})
        .click();
      await page.getByRole('button', {name: /cargo test --release sampler.*exit 1/}).click();
    },
  },
  {
    name: 'steer',
    act: async page => {
      const box = page.getByRole('textbox', {name: 'Steer the next agent call'});
      await box.fill('Measure lock hold time before replacing the queue.');
      await box.press('Enter');
      await page.getByText('Queued for the next agent call').waitFor();
    },
  },
  {
    name: 'pausing',
    act: async page => {
      await page.locator('.titlebar').getByRole('button', {name: 'Pause'}).click();
      await page.getByText('Pausing after the current call…').waitFor();
    },
  },
  {
    name: 'paused',
    act: async (page, gateway) => {
      await page.locator('.titlebar').getByRole('button', {name: 'Pause'}).click();
      // As at runtime: the judge's call finishes, then the pause takes effect.
      gateway.advance(ROUND_6_FINISHED);
      gateway.setStatus('paused');
      await page.getByText('Paused after round 6').waitFor();
    },
  },
  {
    name: 'failed',
    act: async (page, gateway) => {
      gateway.push({
        type: 'run_failed',
        text: 'Benchmark harness crashed',
        diagnostic: {
          code: 'benchmark_crashed',
          summary: 'Benchmark harness crashed',
          detail: 'cargo bench exited with status 137 after 41.7s (killed by the OOM killer).',
          scope: 'run',
          severity: 'fatal',
        },
      });
      await page.getByText('Failed: Benchmark harness crashed').waitFor();
    },
  },
  {
    name: 'stop',
    act: async page => {
      await page.getByRole('button', {name: 'More'}).click();
      await page.getByRole('menuitem', {name: 'Stop run…'}).click();
      await page.getByRole('alertdialog').waitFor();
    },
  },
  {name: 'pane', act: async page => page.getByRole('button', {name: 'Toggle side pane'}).click()},
  {name: 'collapsed', act: async page => page.getByRole('button', {name: 'Hide sidebar'}).click()},
  {
    name: 'ask',
    act: async page => {
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByRole('tab', {name: 'Ask'}).click();
    },
  },
  {
    name: 'changes',
    act: async page => {
      await runs(page)
        .getByRole('button', {name: /^Round 1,/})
        .click();
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.locator('.pbody .diff').first().waitFor();
    },
  },
  {
    name: 'changes-r4',
    act: async page => {
      await runs(page)
        .getByRole('button', {name: /^Round 4,/})
        .click();
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByText('could not produce this patch').waitFor();
    },
  },
  {
    name: 'changes-r3',
    act: async page => {
      await runs(page)
        .getByRole('button', {name: /^Round 3,/})
        .click();
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByRole('button', {name: /more changed lines$/}).click();
      await page.getByText("Patch truncated at the server's size bound.").waitFor();
    },
  },
  {
    name: 'agents',
    act: async page => {
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByRole('tab', {name: 'Agents'}).click();
      await page.getByRole('button', {name: /^Implementer/}).click();
      await page.locator('.filterbar').waitFor();
    },
  },
  {
    name: 'experiments',
    act: async page => {
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByRole('tab', {name: 'Experiments'}).click();
      await page
        .getByRole('complementary', {name: 'Run details'})
        .locator('.xrow', {hasText: 'Skip the post-sampling device sync'})
        .click();
    },
  },
  {
    name: 'design',
    act: async page => {
      await page.getByRole('button', {name: 'Toggle side pane'}).click();
      await page.getByRole('tab', {name: 'Experiments'}).click();
      await page.getByRole('button', {name: 'Design'}).click();
    },
  },
  {
    name: 'palette',
    act: async page => {
      await page.getByRole('button', {name: /Search and commands/}).click();
      await page.getByRole('dialog', {name: 'Search and commands'}).waitFor();
    },
  },
];

test.describe('screens', () => {
  test.skip(OUT === undefined, 'Set CAPTURE_DIR to write the frames');
  for (const screen of SCREENS) {
    test(screen.name, async ({page}) => {
      const gateway = await mockGateway(
        page,
        screen.through === undefined ? {} : {through: screen.through},
      );
      await page.clock.setFixedTime(new Date('2026-09-25T14:02:00Z'));
      await page.setViewportSize({width: 1440, height: 900});
      await page.goto('/?token=e2e');
      await page.locator('.titlebar').waitFor();
      await screen.act?.(page, gateway);
      for (const width of [1440, 1024]) {
        for (const theme of ['dark', 'light'] as const) {
          await page.setViewportSize({width, height: 900});
          await page.emulateMedia({colorScheme: theme, reducedMotion: 'reduce'});
          await page.screenshot({path: join(OUT ?? '', `${screen.name}-${width}-${theme}.png`)});
        }
      }
    });
  }
});

const NEW = `${HOME}#new`;
const KEY = 'sk-e2e-0123456789';

interface SetupScreen {
  name: string;
  url: string;
  options?: Partial<HomeOptions>;
  /** Replays the demo run behind GATEWAY_WS, finished at FINISHED. */
  finished?: boolean;
  act: (page: Page) => Promise<void>;
}

const see = (page: Page, text: string) => page.getByText(text).first().waitFor();
const ready = async (page: Page) => {
  await see(page, 'Git repository, working tree clean');
  await page.locator('.summary').waitFor();
};
const provider = (page: Page, name: string) =>
  page.getByRole('combobox', {name: 'Provider'}).selectOption(name);
const pasteKey = (wait: string) => async (page: Page) => {
  await ready(page);
  await provider(page, 'codex');
  const key = page.getByLabel('OpenAI API key');
  await key.fill(KEY);
  await key.press('Enter');
  await see(page, wait);
};
const start = (wait: string) => async (page: Page) => {
  await ready(page);
  await page.getByRole('button', {name: 'Start run'}).click();
  await see(page, wait);
};
const shadowed = {
  ...AUTH,
  providers: AUTH.providers.map(row =>
    row.provider === 'claude'
      ? {...row, keys: [{name: 'ANTHROPIC_API_KEY', source: 'env' as const, shadowed: true}]}
      : row,
  ),
};

const SETUP_SCREENS: SetupScreen[] = [
  {name: 'home', url: HOME, act: page => see(page, 'Select a run, or start one with ⌘N')},
  {name: 'setup', url: NEW, act: ready},
  {name: 'checking', url: NEW, options: {slow: [ROOT]}, act: page => see(page, 'Checking…')},
  {
    name: 'blockers',
    url: NEW,
    options: {validation: {state: 'dirty_tree', pending: ['src/batch.rs']}},
    act: async page => {
      await page.locator('.summary').waitFor();
      await provider(page, 'codex');
      await see(page, 'to fix:');
    },
  },
  {
    name: 'uninitialized',
    url: NEW,
    options: {validation: {state: 'uninitialized', pending: []}, tasks: []},
    act: page => see(page, 'No VibeSys tasks here yet'),
  },
  {
    name: 'new',
    url: NEW,
    act: async page => {
      await ready(page);
      await page.getByLabel('Task').selectOption('+new');
      await page
        .getByLabel('Objective')
        .fill('Increase decode throughput without changing outputs.');
      await page.getByLabel('Benchmark').fill('cargo bench --bench decode');
    },
  },
  {
    name: 'edit',
    url: NEW,
    act: async page => {
      await ready(page);
      await page.getByRole('button', {name: 'Edit'}).click();
    },
  },
  {
    name: 'roles',
    url: NEW,
    act: async page => {
      await ready(page);
      await page.locator('summary', {hasText: 'Use a different model per role'}).click();
      await page.getByLabel('Judge model').fill('claude-haiku-4-5');
    },
  },
  {
    name: 'advanced',
    url: NEW,
    act: async page => {
      await ready(page);
      await page.locator('summary', {hasText: 'Advanced'}).click();
    },
  },
  {
    name: 'picker',
    url: NEW,
    act: async page => {
      await ready(page);
      await page.getByRole('button', {name: 'Browse…'}).click();
      await page.getByRole('dialog').getByRole('button', {name: 'Up'}).click();
      await see(page, 'tokenizer-rs');
    },
  },
  {
    name: 'commit',
    url: NEW,
    options: {validation: {state: 'dirty_tree', pending: ['.vibesys/tasks/decode/OBJECTIVE.md']}},
    act: async page => {
      await page.getByRole('button', {name: 'Commit task files…'}).click();
      await see(page, '.vibesys/tasks/decode/OBJECTIVE.md');
    },
  },
  {name: 'key-saving', url: NEW, options: {hold: ['key']}, act: pasteKey('Saving…')},
  {name: 'key-saved', url: NEW, act: pasteKey('Saved to .env')},
  {name: 'key-rejected', url: NEW, options: {rejectKey: true}, act: pasteKey('Rejected:')},
  {
    name: 'key-shadowed',
    url: NEW,
    options: {auth: shadowed},
    act: page => see(page, 'overrides .env'),
  },
  {
    name: 'cli-signin',
    url: NEW,
    act: async page => {
      await ready(page);
      await provider(page, 'opencode');
      await see(page, 'opencode auth login');
    },
  },
  {name: 'starting', url: NEW, options: {hold: ['attach']}, act: start('Starting the run…')},
  {name: 'launch-failed', url: NEW, options: {start: 'launch_failed'}, act: start('Did not start')},
  {name: 'baseline-failed', url: NEW, options: {start: 'failed'}, act: start('Did not start')},
  {
    name: 'opening',
    url: `${HOME}#open=${PROJECT_ID}/${FINISHED_RUN}`,
    options: {hold: ['open']},
    act: page => see(page, 'Opening the run read-only…'),
  },
  {
    name: 'resume',
    url: `${HOME}#resume=${PROJECT_ID}/${FINISHED_RUN}`,
    act: page => see(page, 'Runs again with the recorded configuration'),
  },
  {
    name: 'run-menu',
    url: runHref('home', PROJECT_ID, GATEWAY_WS),
    finished: true,
    act: async page => {
      await see(page, 'Completed');
      await page.getByRole('button', {name: 'More'}).click();
      await see(page, 'Resume run…');
    },
  },
];

test.describe('setup screens', () => {
  test.skip(OUT === undefined, 'Set CAPTURE_DIR to write the frames');
  for (const screen of SETUP_SCREENS) {
    test(screen.name, async ({page}) => {
      // mockGateway (when used) registers its /api/projects stub first so mockHome's later,
      // broader /api/ handler takes priority for a request both would otherwise match.
      if (screen.finished === true) await mockGateway(page, {through: FINISHED});
      await mockHome(page, screen.options);
      await page.clock.setFixedTime(new Date('2026-09-28T12:00:00Z'));
      await page.setViewportSize({width: 1440, height: 900});
      await page.goto(screen.url);
      await screen.act(page);
      for (const width of [1440, 1024]) {
        for (const theme of ['dark', 'light'] as const) {
          await page.setViewportSize({width, height: 900});
          await page.emulateMedia({colorScheme: theme, reducedMotion: 'reduce'});
          await page.screenshot({
            path: join(OUT ?? '', `setup-${screen.name}-${width}-${theme}.png`),
          });
        }
      }
    });
  }
});
