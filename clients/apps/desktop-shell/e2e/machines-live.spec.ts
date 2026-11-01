import {expect, type Page, test} from '@playwright/test';
import {card, openMachines, resizeTo, WORLDS} from './machines-page.js';
import {hosts} from './subscription-fake.js';

/**
 * The Machines page over a scripted host's live stats: what each state shows on its card, that a
 * change of state reaches the screen without moving what is around it, that Retry is a request
 * main hears and is answered, that a card has one status, and that the page watches only while it
 * is on screen.
 */

const world = (name: string) => {
  const found = WORLDS.find(each => each.name === name);
  if (found === undefined) throw new Error(`no world ${name}`);
  return found;
};

const GIB = 2 ** 30;
const gauges = (page: Page, machine: string) => card(page, machine).locator('.vs-gauge');
const chips = (page: Page, machine: string) => card(page, machine).locator('.vs-chip');
const dimmed = (page: Page, machine: string) => card(page, machine).locator('.vs-gauge[data-dim]');

async function height(page: Page, machine: string): Promise<number> {
  const box = await card(page, machine).boundingBox();
  if (box === null) throw new Error(`${machine} has no box`);
  return box.height;
}

/** The left edge and width of each gauge of a card, as drawn. */
function boxes(page: Page, machine: string): Promise<{x: number; width: number}[]> {
  return gauges(page, machine).evaluateAll(each =>
    each.map(gauge => {
      const {x, width} = gauge.getBoundingClientRect();
      return {x, width};
    }),
  );
}

const BUSY = {cpuPercent: 34, memory: {usedBytes: 18 * GIB, totalBytes: 36 * GIB}};

test('every state keeps the card the same height, at each width', async ({page}) => {
  await openMachines(page, 1440, world('two live machines'));
  const states = [
    {kind: 'live', readings: BUSY},
    {kind: 'stale', readings: BUSY, reason: 'silent', ageMs: 42_000},
    {kind: 'stale', readings: BUSY, reason: 'ended', ageMs: 42_000, attempt: 0},
    {kind: 'connecting', step: 'handshake', attempt: 0},
    {kind: 'failed', reason: 'unreachable', retrying: true},
    {kind: 'failed', reason: 'closed', retrying: false},
    {kind: 'live', readings: {cpuPercent: null, memory: null, gpus: null}},
    {kind: 'live', readings: BUSY},
  ] as const;
  for (const width of [1440, 1000]) {
    await resizeTo(page, width);
    await hosts.set(page, 'local', states[0]);
    await expect(chips(page, 'This Mac')).toHaveText(['Online']);
    const first = await height(page, 'This Mac');
    const second = await height(page, 'host-a');
    for (const feed of states) {
      await hosts.set(page, 'local', feed);
      await hosts.set(page, 'ssh:host-a', feed);
      await expect(dimmed(page, 'This Mac')).toHaveCount(feed.kind === 'stale' ? 2 : 0);
      expect(await height(page, 'This Mac'), `${feed.kind} at ${width}px`).toBeCloseTo(first, 0);
      expect(await height(page, 'host-a'), `${feed.kind} at ${width}px`).toBeCloseTo(second, 0);
    }
  }
});

test('a figure that gains a digit moves nothing beside it', async ({page}) => {
  await openMachines(page, 1440, world('two live machines'));
  const steps = [
    {cpu: null, local: 9.9, host: 9, gpu: 9, vram: 5},
    {cpu: 9, local: 10.2, host: 10, gpu: 10, vram: 52},
    {cpu: 10, local: 35.9, host: 100, gpu: 100, vram: 640},
    {cpu: 100, local: 0, host: 212, gpu: 5, vram: 1},
    {cpu: 0, local: 18, host: 9, gpu: 0, vram: 0},
  ];
  let baseline: {local: {x: number; width: number}[]; host: {x: number; width: number}[]} | null =
    null;
  for (const step of steps) {
    await hosts.set(page, 'local', {
      kind: 'live',
      readings: {
        cpuPercent: step.cpu,
        memory: {usedBytes: Math.round(step.local * GIB), totalBytes: 36 * GIB},
      },
    });
    await hosts.set(page, 'ssh:host-a', {
      kind: 'live',
      readings: {
        cpuPercent: step.cpu,
        memory: {usedBytes: Math.round(step.host * GIB), totalBytes: 512 * GIB},
        gpus: Array.from({length: 8}, () => ({
          name: 'H100',
          utilizationPercent: step.gpu,
          usedBytes: Math.round((step.vram / 8) * GIB),
          totalBytes: 80 * GIB,
        })),
      },
    });
    await expect(gauges(page, 'This Mac').first()).toHaveText(
      step.cpu === null ? 'CPU–' : `CPU${step.cpu}%`,
    );
    await expect(gauges(page, 'host-a').nth(2)).toHaveText(`GPU${step.gpu}%`);
    const now = {local: await boxes(page, 'This Mac'), host: await boxes(page, 'host-a')};
    baseline ??= now;
    for (const key of ['local', 'host'] as const) {
      now[key].forEach((box, at) => {
        expect(
          Math.abs(box.x - (baseline?.[key][at]?.x ?? 0)),
          `${key} gauge ${at} x`,
        ).toBeLessThanOrEqual(1);
        expect(
          Math.abs(box.width - (baseline?.[key][at]?.width ?? 0)),
          `${key} gauge ${at} width`,
        ).toBeLessThanOrEqual(1);
      });
    }
  }
});

/** What the card's summary line shows, in order, and where each cell is. */
async function summary(page: Page, machine: string) {
  const row = card(page, machine).locator('.vs-meter[role="img"]').locator('xpath=../..');
  return row.evaluate(element =>
    [...element.children].map(cell => {
      const {x, width} = cell.getBoundingClientRect();
      return {text: cell.textContent ?? '', x, width};
    }),
  );
}

test('when a host stops delivering the summary line keeps its meter and its run count where they were, dimmed', async ({
  page,
}) => {
  await openMachines(page, 1440, world('a busy machine'));
  const meter = card(page, 'This Mac').locator('.vs-meter[role="img"]');
  await expect(meter).toBeVisible();
  await expect(meter).not.toHaveAttribute('data-dim');
  const live = await summary(page, 'This Mac');
  expect(live.map(each => each.text)).toEqual(['64 CPU · 8× H100', '2 live', '3 runs']);
  const gone = {reason: 'silent', ageMs: 42_000} as const;
  await hosts.set(page, 'local', {kind: 'stale', readings: BUSY, ...gone});
  await hosts.setRuns(page, 'local', {alive: 2, ended: 1, stale: gone});
  await expect(meter).toHaveAttribute('data-dim', 'true');
  // Nothing is removed, its text is the same, and nothing moves.
  expect(await summary(page, 'This Mac')).toEqual(live);
  // The host answers again: the same line, current.
  await hosts.set(page, 'local', {kind: 'live', readings: BUSY});
  await hosts.setRuns(page, 'local', {alive: 2, ended: 1});
  await expect(meter).not.toHaveAttribute('data-dim');
  expect(await summary(page, 'This Mac')).toEqual(live);
});
