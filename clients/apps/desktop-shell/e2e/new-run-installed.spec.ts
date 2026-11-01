import {mkdirSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import type {FakeBridgeSeed} from '../src/testing/fake-bridge.js';
import {KnownLedger} from './checks/known-ledger.js';
import {SCREEN_CHECKS, screenCheck} from './checks/screen-check.js';
import {main, NEW_RUN_PAGES, openAt} from './new-run-flow.js';
import {SEED} from './world.js';

/**
 * The Agent page of a New run on machines that have different harnesses installed. The helper
 * lists Claude Code first on every machine; New run orders the list itself from the machine's
 * description, so the form starts with an installed harness. With none installed it starts as
 * before, and each tile says "Not found".
 * `SCREENS_DIR=<dir>` writes each window's screenshot (the app window only).
 */

const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const ledger = new KnownLedger('new-run');

const AGENT = NEW_RUN_PAGES.find(each => each.name === 'agent');
if (AGENT === undefined) throw new Error('no New run page named agent');

/** The world whose machine has `installed` harness CLIs, the helper listing Claude Code first. */
function installing(installed: readonly string[]): FakeBridgeSeed {
  const newRun = SEED.newRun;
  if (newRun === undefined) throw new Error('the seed has no New run helper');
  const machine = newRun.machine as Record<string, unknown>;
  return {...SEED, newRun: {...newRun, machine: {...machine, harness_clis: [...installed]}}};
}

const WORLDS = [
  {
    name: 'all installed',
    installed: ['claude', 'codex'],
    line: 'Anthropic',
    harness: 'Claude Code',
  },
  {name: 'codex only', installed: ['codex'], line: 'OpenAI', harness: 'Codex'},
  {name: 'none installed', installed: [], line: 'Anthropic', harness: 'Claude Code'},
] as const;

for (const width of [1440, 1000] as const) {
  for (const world of WORLDS) {
    test(`${world.name}: the form starts with ${world.line} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 900});
      await openAt(page, AGENT, installing(world.installed));

      const lines = main(page).getByRole('radiogroup', {name: 'Model line'});
      await expect(lines.getByRole('radio', {name: world.line, exact: true})).toBeChecked();
      const harness = main(page).getByRole('radiogroup', {name: 'Harness'});
      await expect(harness.getByRole('radio', {name: new RegExp(world.harness)})).toBeChecked();

      const installed = world.installed.length > 0;
      await expect(harness).toContainText(
        installed ? 'Installed on host-b' : 'Not found on host-b',
      );
      // Nothing installed is the tile's own words, "Not found on host-b": no chip, sentence or banner.
      await expect(main(page).getByText('None installed')).toHaveCount(0);

      // The installed harness's model line is the first card; the helper's own order is not.
      if (world.name === 'codex only') {
        await expect(lines.getByRole('radio').first()).toHaveAccessibleName('OpenAI');
      }

      const name = `new-run-installed/${world.name.replaceAll(' ', '-')}/${width}`;
      expect(ledger.unexplained(name, await screenCheck(page, SCREEN_CHECKS))).toEqual([]);
      if (shots !== undefined) {
        // The app window, never the desktop: the page the renderer draws.
        await page.screenshot({
          path: `${shots}/new-run-installed__${world.name.replaceAll(' ', '-')}__${width}.png`,
        });
      }
    });
  }
}

test('a harness the person picked stays when the machine reports what it has', async ({page}) => {
  await openAt(page, AGENT, installing(['codex']));
  const lines = main(page).getByRole('radiogroup', {name: 'Model line'});
  await expect(lines.getByRole('radio', {name: 'OpenAI', exact: true})).toBeChecked();
  await lines.getByRole('radio', {name: 'Anthropic', exact: true}).check();
  await expect(lines.getByRole('radio', {name: 'Anthropic', exact: true})).toBeChecked();
  const harness = main(page).getByRole('radiogroup', {name: 'Harness'});
  await expect(harness.getByRole('radio', {name: /Claude Code/})).toBeChecked();
  await expect(harness).toContainText('Not found on host-b');
});
