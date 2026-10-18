import {mkdirSync, writeFileSync} from 'node:fs';
import {expect, type Page, test} from '@playwright/test';
import {contactSheetHtml, type SheetEntry} from './checks/contact-sheet.js';
import {screenCheck} from './checks/screen-check.js';
import {installFakeBridge, installFakeDigests, installFakeMachines} from './fake-bridge.js';
import {listed, SEED, sidebar} from './world.js';

/**
 * Recent runs that can be told apart: fourteen runs of one task, another project's runs among
 * them, runs that failed, stopped, were never recorded as ended and one whose state cannot be read.
 * Each row says when the run began and how it came out, runs of one machine sit under one header,
 * no two neighbours read the same, and a run whose state cannot be read says so with a retry.
 * `SIDEBAR_RECENT_DIR` names a directory for the window's screenshots and a contact sheet.
 */

const NOW = Date.parse('2026-10-17T12:00:00Z');
const HOUR = 3_600_000;
const QUEUE = '/home/me/projects/queue-rs';
const STORE = '/home/me/projects/store';

interface Case {
  readonly id: string;
  readonly project: string;
  readonly task: string;
  /** Hours before now that the run began. */
  readonly ago: number;
  readonly ideas: number;
  readonly ratio: number | null;
  readonly minutes: number;
  readonly outcome: string | null;
  readonly closing: string | null;
}

const done = (index: number): Case => ({
  id: `run-${String(index).padStart(2, '0')}`,
  project: QUEUE,
  task: 'mpmc',
  ago: 2 + index * 3.7,
  ideas: 4 + index,
  ratio: 1.2 + index * 0.17,
  minutes: 20 + index * 4,
  outcome: 'adopted',
  closing: 'finished',
});

const CASES: readonly Case[] = [
  done(0),
  done(1),
  {...done(2), outcome: 'stopped', closing: 'interrupted', ideas: 2, ratio: 1.1},
  {...done(3), outcome: 'failed', closing: 'failed', ideas: 0, ratio: null, minutes: 1},
  {...done(4), project: STORE, task: 'lookup'},
  {...done(5), project: STORE, task: 'lookup', outcome: 'no-winner', ratio: null},
  done(6),
  {...done(7), outcome: null, closing: null},
  done(8),
  done(9),
  {...done(10), outcome: 'deadline', ratio: 1.8},
  done(11),
  done(12),
  done(13),
];
/** Never reported on: the host has no record of it. */
const MISSING = {...done(14), id: 'run-missing'};
/** Reported as unreadable. */
const UNREADABLE = {...done(15), id: 'run-unreadable'};
const ALL = [...CASES, MISSING, UNREADABLE];

const entry = (each: Case): unknown => ({
  kind: 'read',
  createdAt: NOW - each.ago * HOUR,
  ideas: each.ideas,
  adoptedRatio: each.ratio,
  durationSeconds: each.minutes * 60,
  outcome: each.outcome,
  closing: each.closing,
  over: each.outcome !== null || each.closing !== null,
});

const DIGESTS: Record<string, unknown> = {
  ...Object.fromEntries(CASES.map(each => [each.id, entry(each)])),
  [UNREADABLE.id]: {kind: 'unreadable', createdAt: NOW - UNREADABLE.ago * HOUR, message: 'x'},
};

const WORLD = {
  ...SEED,
  listings: [listed([], [])],
  recent: ALL.map((each, index) => ({
    host: 'ssh:host-b',
    hostLabel: 'host-b',
    project: each.project,
    task: each.task,
    runId: each.id,
    instanceId: null,
    attachedAt: 1000 - index,
  })),
  digests: DIGESTS,
};

const shots = process.env['SIDEBAR_RECENT_DIR'] || undefined;
if (shots !== undefined) mkdirSync(shots, {recursive: true});
const sheet: SheetEntry[] = [];

async function open(page: Page): Promise<void> {
  await page.clock.setFixedTime(NOW);
  await page.addInitScript(installFakeBridge, WORLD);
  await page.addInitScript(installFakeMachines, WORLD);
  await page.addInitScript(installFakeDigests, WORLD);
  await page.goto('./shell.html');
  await expect(sidebar(page)).toBeVisible();
  await expect(sidebar(page).getByText('reading…')).toHaveCount(0);
  // The main area shows a screen that is not another journey's subject.
  await sidebar(page)
    .getByRole('button', {name: /^Show all/})
    .click();
}

const rows = (page: Page) => sidebar(page).locator('section[aria-label="Recent"] .vs-sidebar-item');

test.use({timezoneId: 'UTC', colorScheme: 'dark'});

for (const width of [1000, 1920]) {
  test(`recent runs differ and pass the checks at ${width}`, async ({page}) => {
    await page.setViewportSize({width, height: 1400});
    await open(page);

    const texts = await rows(page).evaluateAll(items =>
      items.map(item => (item as HTMLElement).innerText.replace(/\s+/g, ' ').trim()),
    );
    expect(texts).toHaveLength(ALL.length);
    texts.forEach((text, index) => {
      if (index > 0)
        expect(text, `row ${index} repeats row ${index - 1}`).not.toBe(texts[index - 1]);
    });
    expect(texts.join('\n')).not.toContain('status unknown');
    expect(texts[0]).toMatch(/^queue-rs · mpmc \d\d:\d\d · 1\.\d\d×/);
    expect(texts.some(text => /Yesterday \d\d:\d\d/.test(text))).toBe(true);
    expect(texts.some(text => /Oct \d+ · /.test(text))).toBe(true);
    expect(texts.some(text => /stopped · 2 ideas/.test(text))).toBe(true);
    expect(texts.some(text => text.includes('stopped unexpectedly'))).toBe(true);
    expect(texts.some(text => text.includes('failed to start'))).toBe(true);

    const headers = await sidebar(page).locator('.vs-sidebar-group-title').allInnerTexts();
    expect(headers.map(text => text.split('\n')[0])).toEqual(['host-b']);

    await expect(sidebar(page).getByText("can't read run state")).toHaveCount(1);
    await expect(sidebar(page).getByText('this run is gone')).toHaveCount(1);
    await expect(sidebar(page).getByRole('button', {name: 'Retry'})).toHaveCount(1);

    const violations = await screenCheck(page);
    expect(violations.map(each => `[${each.check}] ${each.message} (${each.where})`)).toEqual([]);
    if (shots !== undefined) {
      const file = `sidebar-recent-${width}.png`;
      await page.screenshot({path: `${shots}/${file}`});
      sheet.push({name: `recent runs/sidebar-shown/${width}`, file});
    }
  });
}

test('sidebar controls remain clickable and New run stays pinned while runs scroll', async ({
  page,
}) => {
  await page.setViewportSize({width: 1000, height: 650});
  await open(page);
  const nav = sidebar(page);
  const newRun = nav.getByRole('button', {name: 'New run'});
  const settings = nav.getByRole('button', {name: 'Settings'});
  const before = await newRun.boundingBox();
  const settingsBefore = await settings.boundingBox();
  expect(before).not.toBeNull();
  await nav.locator('.vs-app-nav-top').evaluate(element => {
    element.scrollTop = element.scrollHeight;
  });
  const after = await newRun.boundingBox();
  expect(after?.y).toBe(before?.y);
  expect((await settings.boundingBox())?.y).toBe(settingsBefore?.y);
  await nav
    .getByRole('button', {name: /More for/})
    .first()
    .click();
  await expect(page.getByRole('menuitem', {name: 'Archive'})).toBeVisible();
  await page.getByRole('button', {name: 'Hide sidebar'}).click();
  await expect(nav).toBeHidden();
  await page.getByRole('button', {name: 'Show sidebar'}).click();
  await expect(nav).toBeVisible();
});

test('dragging the sidebar resizes it and a narrow drag collapses it', async ({page}) => {
  await page.setViewportSize({width: 1000, height: 650});
  await open(page);
  const nav = sidebar(page);
  const grip = nav.getByRole('separator', {name: 'Resize sidebar'});
  const box = await grip.boundingBox();
  expect(box).not.toBeNull();
  if (box === null) return;
  await page.mouse.move(box.x + box.width / 2, box.y + 20);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 80, box.y + 20);
  await page.mouse.up();
  await expect
    .poll(() => nav.evaluate(element => Math.round(element.getBoundingClientRect().width)))
    .toBeGreaterThan(300);
  const wider = await grip.boundingBox();
  expect(wider).not.toBeNull();
  if (wider === null) return;
  await page.mouse.move(wider.x + wider.width / 2, wider.y + 20);
  await page.mouse.down();
  await page.mouse.move(60, wider.y + 20);
  await page.mouse.up();
  await expect(nav).toBeHidden();
});

test('the contact sheet shows every recent-runs screen', async ({browser}) => {
  test.skip(shots === undefined, 'set SIDEBAR_RECENT_DIR to write the screenshots');
  if (shots === undefined) return;
  writeFileSync(`${shots}/contact-sheet.html`, contactSheetHtml(sheet, 3));
  const page = await browser.newPage({viewport: {width: 2400, height: 1200}});
  await page.goto(`file://${shots}/contact-sheet.html`);
  await page.screenshot({path: `${shots}/contact-sheet.png`, fullPage: true});
  await page.close();
});
