import {expect, type Page, test} from '@playwright/test';
import {card, openMachines, rail, resizeTo, WORLDS} from './machines-page.js';
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

test('a live host shows its figures, and a change of state reaches its card', async ({page}) => {
  await openMachines(page, 1440, world('a live machine'));
  await expect(gauges(page, 'This Mac')).toHaveText(['CPU34%', 'Memory18.0 / 36.0 GB']);
  await expect(chips(page, 'This Mac')).toHaveText(['Online']);
  await expect(dimmed(page, 'This Mac')).toHaveCount(0);
  await hosts.set(page, 'local', {
    kind: 'stale',
    reason: 'silent',
    ageMs: 125_000,
    readings: {cpuPercent: 80},
  });
  // Dimmed and never hidden, with how old the value is and the status that says so.
  await expect(dimmed(page, 'This Mac')).toHaveCount(2);
  await expect(gauges(page, 'This Mac').first()).toHaveText('CPU80%');
  await expect(card(page, 'This Mac').getByText('2 min ago')).toBeVisible();
  await expect(chips(page, 'This Mac')).toHaveText(['Stale']);
  await hosts.set(page, 'local', {kind: 'live', readings: {cpuPercent: 12}});
  await expect(gauges(page, 'This Mac').first()).toHaveText('CPU12%');
  await expect(dimmed(page, 'This Mac')).toHaveCount(0);
  await expect(card(page, 'This Mac').getByText(/ ago$/)).toHaveCount(0);
  await expect(chips(page, 'This Mac')).toHaveText(['Online']);
});

test('a host with GPUs shows their use and memory beside CPU and memory', async ({page}) => {
  await openMachines(page, 1440, world('two live machines'));
  await expect(gauges(page, 'host-a')).toHaveText([
    'CPU71%',
    'Memory212 / 512 GB',
    'GPU68%',
    'VRAM416 / 640 GB',
  ]);
  await expect(gauges(page, 'This Mac')).toHaveCount(2);
});

test('a card has one status, whatever its stream does, and never Online beside a failure', async ({
  page,
}) => {
  await openMachines(page, 1440, world('a live machine'));
  const states = [
    [{kind: 'live', readings: BUSY}, 'Online'],
    [{kind: 'stale', readings: BUSY, reason: 'silent', ageMs: 42_000}, 'Stale'],
    [{kind: 'stale', readings: BUSY, reason: 'ended', ageMs: 42_000, attempt: 0}, 'Connecting'],
    [{kind: 'connecting', step: 'gateway', attempt: 0}, 'Connecting'],
    [{kind: 'failed', reason: 'unreachable', retrying: true}, 'Unreachable'],
    [{kind: 'failed', reason: 'ended', retrying: true}, 'Disconnected'],
    [{kind: 'failed', reason: 'unsupported', retrying: false}, 'Not supported'],
    [{kind: 'failed', reason: 'closed', retrying: false}, 'Closed'],
  ] as const;
  for (const [feed, word] of states) {
    await hosts.set(page, 'local', feed);
    await expect(chips(page, 'This Mac')).toHaveText([word]);
    if (word !== 'Online') await expect(card(page, 'This Mac').getByText('Online')).toHaveCount(0);
    // Retry is offered where it can help, and not on a host that was removed.
    const offered = feed.kind === 'failed' && feed.reason !== 'closed';
    await expect(
      card(page, 'This Mac').getByRole('button', {name: 'Retry', exact: false}),
    ).toHaveCount(offered ? 1 : 0);
  }
});

test('an older host is Not supported once, and not also Needs update', async ({page}) => {
  await openMachines(page, 1440, world('an older machine'));
  await expect(chips(page, 'host-c')).toHaveText(['Not supported']);
  await expect(card(page, 'host-c').getByText('Needs update')).toHaveCount(0);
  await expect(chips(page, 'This Mac')).toHaveText(['Online']);
});

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

test('a failed host says why in its status, and Retry is answered with the last values, dimmed, until the stream returns', async ({
  page,
}) => {
  await openMachines(page, 1440, world('a failed machine'));
  const mac = card(page, 'This Mac');
  await expect(chips(page, 'This Mac')).toHaveText(['Unreachable']);
  await expect(gauges(page, 'This Mac')).toHaveCount(0);
  // A real button, named for its machine, with the visible text `Retry`.
  const retry = mac.getByRole('button', {name: 'Retry This Mac'});
  await expect(retry).toHaveText('Retry');
  // The header status is the card's one status; nothing is a banner.
  await expect(page.locator('[role="alert"]')).toHaveCount(0);
  await retry.click();
  await expect.poll(() => hosts.retried(page)).toEqual(['local']);
  await expect(chips(page, 'This Mac')).toHaveText(['Connecting']);
  await expect(dimmed(page, 'This Mac')).toHaveCount(2);
  await expect(mac.getByText(/\d+ (s|min) ago/)).toBeVisible();
  await expect(mac.getByRole('button', {name: /Retry/})).toHaveCount(0);
  await hosts.set(page, 'local', {kind: 'live', readings: BUSY});
  await expect(chips(page, 'This Mac')).toHaveText(['Online']);
  await expect(dimmed(page, 'This Mac')).toHaveCount(0);
  // Failed again: the host still does not answer, and Retry is there to be pressed once more.
  await hosts.set(page, 'local', {kind: 'failed', reason: 'unreachable', retrying: true});
  await expect(chips(page, 'This Mac')).toHaveText(['Unreachable']);
  await mac.getByRole('button', {name: 'Retry This Mac'}).click();
  await expect(chips(page, 'This Mac')).toHaveText(['Connecting']);
  await hosts.set(page, 'local', {kind: 'failed', reason: 'unreachable', retrying: true});
  await expect(chips(page, 'This Mac')).toHaveText(['Unreachable']);
  await expect(mac.getByRole('button', {name: 'Retry This Mac'})).toBeVisible();
});

test('each failed host has a Retry named for it, and pressing one asks only that host', async ({
  page,
}) => {
  await openMachines(page, 1440, world('four machines in every state'));
  await expect(card(page, 'host-b').getByRole('button', {name: 'Retry host-b'})).toHaveText(
    'Retry',
  );
  await expect(page.getByRole('button', {name: /^Retry/})).toHaveCount(1);
  await card(page, 'host-b').getByRole('button', {name: 'Retry host-b'}).click();
  await expect.poll(() => hosts.retried(page)).toEqual(['ssh:host-b']);
});

test('a meter is announced with its value, as unknown when unread, and as stale when it is', async ({
  page,
}) => {
  await openMachines(page, 1440, world('a live machine'));
  const cpu = card(page, 'This Mac').getByRole('meter', {name: 'CPU'});
  await expect(cpu).toHaveAttribute('aria-valuenow', '34');
  await expect(cpu).toHaveAttribute('aria-valuetext', '34%');
  await hosts.set(page, 'local', {
    kind: 'stale',
    reason: 'silent',
    ageMs: 42_000,
    readings: {cpuPercent: 34, memory: null},
  });
  await expect(cpu).toHaveAttribute('aria-valuetext', /^34%, stale \d+ s$/);
  // A reading that was not made is an unknown by name, not a meter at zero, and stale as well.
  const memory = card(page, 'This Mac').getByRole('img', {name: /^Memory unknown, stale \d+ s$/});
  await expect(memory).toBeVisible();
  await expect(card(page, 'This Mac').getByRole('meter', {name: 'Memory'})).toHaveCount(0);
});

test('hot is the warn tone, and a reading that is not hot is not', async ({page}) => {
  await openMachines(page, 1440, world('a live machine'));
  const fill = (name: string) =>
    card(page, 'This Mac').getByRole('meter', {name}).locator('.vs-meter');
  await expect(fill('CPU')).toHaveAttribute('data-tone', 'ok');
  await hosts.set(page, 'local', {kind: 'live', readings: {...BUSY, cpuPercent: 95}});
  await expect(fill('CPU')).toHaveAttribute('data-tone', 'warn');
  await hosts.set(page, 'local', {kind: 'live', readings: {...BUSY, cpuPercent: 89}});
  await expect(fill('CPU')).toHaveAttribute('data-tone', 'ok');
});

test('the page watches a host only while it is on screen', async ({page}) => {
  await openMachines(page, 1440, world('a live machine'));
  await expect.poll(() => hosts.observing(page, 'local')).toBe(1);
  await rail(page).getByRole('button', {name: 'Runs'}).click();
  await expect.poll(() => hosts.observing(page, 'local')).toBe(0);
  await rail(page).getByRole('button', {name: 'Machines'}).click();
  await expect.poll(() => hosts.observing(page, 'local')).toBe(1);
  await expect(gauges(page, 'This Mac')).toHaveCount(2);
});
