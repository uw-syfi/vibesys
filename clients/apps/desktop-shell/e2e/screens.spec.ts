import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, test} from '@playwright/test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {KnownLedger} from './checks/known-ledger.js';
import {IN_PAGE_CHECKS, SCREEN_CHECKS, screenCheck} from './checks/screen-check.js';
import {openFixture, playLive} from './fixture-world.js';
import {type Journey, walkScreens} from './screens.js';

/**
 * Every screen of the Run screen, checked against its real layout: run, idea, step and item
 * levels, the Timeline, Agents and Usage faces and the scrubbed run, live and finished, at 1000,
 * 1440 and 1920 px; and the stress fixtures (long titles, every failure
 * reason, retries, rounds, bases that are not the input, a reconnect, 600 ideas over 14 days) at
 * 1000 and 1440. Each screen must pass the layout and wording checks (`checks/screen-check.ts`)
 * except for the defects listed in `checks/known-failures.ts`, and the performance chart and time
 * bar must keep their element and place from level to level. `SCREENS_DIR=<dir>` also writes each
 * screen's screenshot (the app window only) and a contact sheet of them.
 */

const DEEP: Journey = {
  idea: 'Combine W1 ring with W2 padding',
  command: 'cargo bench --bench queue',
  judge: true,
  live: true,
};
const SHALLOW: Journey = {idea: null, command: null, judge: false, live: true};

interface Cell {
  readonly fixture: string;
  readonly journey: Journey;
  readonly width: number;
  readonly theme: 'ink' | 'paper';
}

const FINISHED: Readonly<Record<string, Journey>> = {
  'quota-pause': {idea: 'Pin the consumers to cores', command: null, judge: false, live: true},
  'crashed-after-turn': {idea: null, command: null, judge: false, live: false},
  'finished-run': {...DEEP, idea: 'Combine the ring with the padding', live: false},
  'finished-ten-ideas': {idea: 'Pad the slot headers', command: null, judge: false, live: false},
  'finished-two-objectives': {
    idea: 'Pad the slot headers',
    command: null,
    judge: false,
    live: false,
  },
};

const MAIN: readonly Cell[] = [
  'queue-rs-live',
  'finished-run',
  'finished-ten-ideas',
  'finished-two-objectives',
].flatMap(fixture =>
  [1000, 1440, 1920].map(width => ({
    fixture,
    journey: FINISHED[fixture] ?? DEEP,
    width,
    theme: 'ink' as const,
  })),
);
const STRESS: readonly Cell[] = [
  'long-titles',
  'failure-reasons',
  'retries',
  'continuation-rounds',
  'non-input-bases',
  'bases-and-effects',
  'quota-pause',
  'input-gate-failed',
  'comparison-baselines',
  'live-reconnect',
  'crashed-after-turn',
  'weeks-600-ideas',
].flatMap(fixture =>
  [1000, 1440].map(width => ({
    fixture,
    journey: FINISHED[fixture] ?? SHALLOW,
    width,
    theme: 'ink' as const,
  })),
);
const LIGHT: readonly Cell[] = [
  {fixture: 'queue-rs-live', journey: DEEP, width: 1440, theme: 'paper'},
];
const CELLS: readonly Cell[] = [...MAIN, ...STRESS, ...LIGHT];

const ledger = new KnownLedger('run-matrix');

/**
 * The fixtures whose titles are written in scripts and emoji on purpose, to test how long text
 * wraps: no bundled face has those glyphs, and they are the run's own text, not the UI's, so they
 * skip the bundled-font check.
 */
const OTHER_SCRIPTS: ReadonlySet<string> = new Set(['long-titles']);

let walked = 0;
const sheet: SheetEntry[] = [];
const shots = process.env['SCREENS_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});

function nameOf(cell: Cell, screen: string): string {
  const theme = cell.theme === 'paper' ? '/paper' : '';
  return `${cell.fixture}/${screen}/${cell.width}${theme}`;
}

for (const cell of CELLS) {
  test(`${nameOf(cell, '*')}`, async ({page}) => {
    await page.setViewportSize({width: cell.width, height: 900});
    const fixture = fixtureNamed(cell.fixture);
    await openFixture(page, fixture, {theme: cell.theme});
    await playLive(page, fixture);
    const unexplained: string[] = [];
    const moved = await walkScreens(page, cell.journey, async (screen, current) => {
      const name = nameOf(cell, screen);
      unexplained.push(
        ...ledger.unexplained(
          name,
          await screenCheck(
            current,
            OTHER_SCRIPTS.has(cell.fixture) ? IN_PAGE_CHECKS : SCREEN_CHECKS,
          ),
        ),
      );
      if (shots !== undefined) {
        const file = `${name.replaceAll('/', '__')}.png`;
        await current.screenshot({path: `${shots}/${file}`});
        sheet.push({name, file});
      }
    });
    const lines = [...unexplained, ...ledger.unexplained(nameOf(cell, 'walk'), moved)];
    walked += 1;
    expect(lines, `screens that break the layout or wording checks:\n${lines.join('\n')}`).toEqual(
      [],
    );
  });
}

ledger.requireEveryFailure(() => walked >= CELLS.length);

test('the contact sheet shows every screen', async ({browser}) => {
  test.skip(shots === undefined, 'set SCREENS_DIR to write the screenshots and the contact sheet');
  if (shots === undefined) return;
  writeFileSync(`${shots}/contact-sheet.html`, contactSheetHtml(sheet, 6));
  const page = await browser.newPage({viewport: {width: 2400, height: 1200}});
  await page.goto(`file://${shots}/contact-sheet.html`);
  await page.screenshot({path: `${shots}/contact-sheet.png`, fullPage: true});
  await page.close();
});
