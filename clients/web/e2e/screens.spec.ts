/**
 * Screenshots of the run window and every setup screen for review: CAPTURE_DIR=<dir> pnpm
 * --filter @vibesys/web exec playwright test screens. Each screen is written at 1440 and 1024,
 * dark and light, as <name>-<width>-<theme>.png (setup screens: setup-<name>-<width>-<theme>.png).
 * Without CAPTURE_DIR the spec is skipped (CI runs app.spec and setup.spec only).
 */
import {join} from 'node:path';
import {type Page, test} from '@playwright/test';
import {runHref} from '../src/route.js';
import {FINISHED, type Gateway, mockGateway, mockNotes, ROUND_6_FINISHED} from './gateway.js';
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
  chat?: 'off' | 'error' | 'held' | 'unanswered';
  notes?: 'fail';
  /** Default `/?token=e2e`; Notes need a run page the home server opened (a gateway link). */
  url?: string;
  act?: (page: Page, gateway: Gateway) => Promise<void>;
}

const runs = (page: Page) => page.getByRole('navigation', {name: 'Runs'});
const openPane = async (page: Page, tab: string) => {
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  await page
    .getByRole('complementary', {name: 'Run details'})
    .getByRole('tab', {name: tab})
    .click();
};
const sendQuestion = async (page: Page) => {
  await openPane(page, 'Ask');
  const box = page.getByRole('textbox', {name: 'Ask about this run'});
  await box.fill('Why did round 3 fail the judge?');
  await box.press('Enter');
};
const askQuestion = async (page: Page) => {
  await sendQuestion(page);
  await page.getByText(/^Mock reply\./).waitFor();
};

/** [mock] The mockup's note text. */
const NOTE =
  "Round 4 traded peak throughput for the buffer pool that round 5 needed. Check p99 latency before keeping round 7's admission delay.";

const HOME_RUN = '/?token=e2e&gateway=/';

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
  {name: 'theme-menu', act: async page => page.getByRole('button', {name: 'More'}).click()},
  {name: 'pane', act: async page => page.getByRole('button', {name: 'Toggle side pane'}).click()},
  {name: 'collapsed', act: async page => page.getByRole('button', {name: 'Hide sidebar'}).click()},
  {
    name: 'ask-empty',
    act: async page => {
      await openPane(page, 'Ask');
      await page.getByRole('textbox', {name: 'Ask about this run'}).waitFor();
    },
  },
  {
    name: 'ask-asking',
    chat: 'held',
    act: async page => {
      await sendQuestion(page);
      await page.getByText('Answering…').waitFor();
    },
  },
  {name: 'ask', act: askQuestion},
  {
    name: 'ask-thread',
    act: async page => {
      await askQuestion(page);
      await page.getByRole('button', {name: /^Thread:/}).click();
    },
  },
  {
    name: 'ask-model',
    act: async page => {
      await askQuestion(page);
      await page.getByRole('button', {name: 'Chat model'}).click();
    },
  },
  {
    name: 'ask-nochat',
    chat: 'off',
    act: async page => {
      await openPane(page, 'Ask');
      await page.getByText('This run offers no chat harness.').waitFor();
    },
  },
  {
    name: 'ask-checking',
    chat: 'unanswered',
    act: async page => {
      await openPane(page, 'Ask');
      await page.getByText('Checking the chat harness…').waitFor();
    },
  },
  {
    name: 'ask-failed',
    chat: 'error',
    act: async page => {
      await openPane(page, 'Ask');
      await page.getByText('Couldn’t check the chat harness.').waitFor();
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
  {
    name: 'palette-ask',
    act: async page => {
      await page.getByRole('button', {name: /Search and commands/}).click();
      await page.keyboard.type('thread');
      await page.getByRole('option', {name: /Switch thread…/}).waitFor();
    },
  },
  {
    name: 'palette-theme',
    act: async page => {
      await page.getByRole('button', {name: /Search and commands/}).click();
      await page.keyboard.type('theme');
    },
  },
  {name: 'notes', url: HOME_RUN, act: async page => openPane(page, 'Notes')},
  {name: 'notes-failed', url: HOME_RUN, notes: 'fail', act: async page => openPane(page, 'Notes')},
];

const mockScreenNotes = (page: Page, screen: Screen) =>
  screen.notes === 'fail'
    ? page.route('**/api/notes/*', route => route.fulfill({status: 404, body: 'Not found'}))
    : mockNotes(page, NOTE);

test.describe('screens', () => {
  test.skip(OUT === undefined, 'Set CAPTURE_DIR to write the frames');
  for (const screen of SCREENS) {
    test(screen.name, async ({page}) => {
      const gateway = await mockGateway(page, {
        ...(screen.through === undefined ? {} : {through: screen.through}),
        ...(screen.chat === undefined ? {} : {chat: screen.chat}),
      });
      await page.clock.setFixedTime(new Date('2026-09-25T14:02:00Z'));
      await page.setViewportSize({width: 1440, height: 900});
      await mockScreenNotes(page, screen);
      await page.goto(screen.url ?? '/?token=e2e');
      await page.locator('.titlebar').waitFor();
      await screen.act?.(page, gateway);
      for (const width of [1440, 1024]) {
        for (const theme of ['dark', 'light'] as const) {
          await page.setViewportSize({width, height: 900});
          await page.emulateMedia({colorScheme: theme, reducedMotion: 'reduce'});
          await page.screenshot({
            path: join(OUT ?? '', `${screen.name}-${width}-${theme}.png`),
            animations: 'disabled',
          });
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
  {
    name: 'home',
    url: HOME,
    act: page => see(page, 'Select a run, or choose New run in the sidebar'),
  },
  {
    name: 'home-menu',
    url: HOME,
    act: async page => {
      await see(page, 'Select a run, or choose New run in the sidebar');
      await page.getByRole('button', {name: 'More'}).click();
      await page.getByRole('menuitemradio', {name: 'System'}).waitFor();
    },
  },
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
  {name: 'key-saved', url: NEW, act: pasteKey('Saved in .env')},
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
  {
    name: 'palette',
    url: HOME,
    act: async page => {
      await page.getByRole('button', {name: /Search and commands/}).click();
      await page.keyboard.type('theme');
      await page
        .getByRole('option', {name: /Theme:/})
        .first()
        .waitFor();
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
            animations: 'disabled',
          });
        }
      }
    });
  }
});
