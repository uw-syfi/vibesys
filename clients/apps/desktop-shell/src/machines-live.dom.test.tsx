/**
 * The Machines page over a scripted host's live stats: what each state shows on its card, that a
 * change of state reaches the screen, that Retry is a request main hears and is answered, that a
 * card has one status, and that the page watches only while it is on screen. Drawn in a DOM; the
 * browser keeps how the cards sit (the same height in every state, a figure that gains a digit,
 * the summary line's cells).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {press} from './testing/dom-controls.js';
import {all, maybe, one, see, settle, textOf, until} from './testing/dom-query.js';
import {cardOf, openMachines, rail, warning, world} from './testing/machines-driver.js';
import type {MachinesWorld} from './testing/machines-world.js';
import type {OpenShell} from './testing/shell-world.js';
import type {HostFeed, RunsFeed} from './testing/subscription-preload.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const shells: OpenShell[] = [];
afterEach(async () => {
  for (const shell of shells.splice(0)) await shell.close();
});

async function open(name: string): Promise<OpenShell> {
  const scripted: MachinesWorld = world(name);
  const shell = await openMachines(scripted, new FakeClock().schedule);
  shells.push(shell);
  return shell;
}

interface Hosts {
  set(host: string, feed: HostFeed): void;
  setRuns(host: string, feed: RunsFeed): void;
  retried(): string[];
  observing(host: string, arm?: 'machineStats' | 'runs'): number;
}
const hosts = (): Hosts =>
  (globalThis.window as unknown as {fakeSubscriptions: Hosts}).fakeSubscriptions;

/** A host's stats change, as main sends a change. */
async function set(host: string, feed: HostFeed): Promise<void> {
  await act(async () => hosts().set(host, feed));
  await settle();
}

const GIB = 2 ** 30;
const BUSY = {cpuPercent: 34, memory: {usedBytes: 18 * GIB, totalBytes: 36 * GIB}};

const gauges = (shell: OpenShell, machine: string): string[] =>
  [...cardOf(shell, machine).querySelectorAll('.vs-gauge')].map(each => textOf(each));
const chips = (shell: OpenShell, machine: string): string[] =>
  [...cardOf(shell, machine).querySelectorAll('.vs-chip')].map(each => textOf(each));
const dimmed = (shell: OpenShell, machine: string): number =>
  cardOf(shell, machine).querySelectorAll('.vs-gauge[data-dim]').length;

/** Returns once the card shows exactly the chips `words`. */
const chipsAre = (shell: OpenShell, machine: string, words: string[]) =>
  until(`${machine} to read ${words.join(', ')}`, () => {
    const found = chips(shell, machine);
    return found.length === words.length && found.every((each, at) => each === words[at]);
  });

test('a live host shows its figures, and a change of state reaches its card', async () => {
  const shell = await open('a live machine');
  expect(gauges(shell, 'This Mac')).toEqual(['CPU34%', 'Memory18.0 / 36.0 GB']);
  expect(chips(shell, 'This Mac')).toEqual(['Online']);
  expect(dimmed(shell, 'This Mac')).toBe(0);
  await set('local', {kind: 'stale', reason: 'silent', ageMs: 125_000, readings: {cpuPercent: 80}});
  // Dimmed and never hidden, with how old the value is and the status that says so.
  expect(dimmed(shell, 'This Mac')).toBe(2);
  expect(gauges(shell, 'This Mac')[0]).toBe('CPU80%');
  await see(cardOf(shell, 'This Mac'), '2 min ago');
  expect(chips(shell, 'This Mac')).toEqual(['Stale']);
  await set('local', {kind: 'live', readings: {cpuPercent: 12}});
  expect(gauges(shell, 'This Mac')[0]).toBe('CPU12%');
  expect(dimmed(shell, 'This Mac')).toBe(0);
  expect(textOf(cardOf(shell, 'This Mac'))).not.toMatch(/ ago$/);
  expect(chips(shell, 'This Mac')).toEqual(['Online']);
});

test('a card has one status, whatever its stream does, and never Online beside a failure', async () => {
  const shell = await open('a live machine');
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
    await set('local', feed);
    await chipsAre(shell, 'This Mac', [word]);
    if (word !== 'Online') {
      expect(textOf(cardOf(shell, 'This Mac')), word).not.toContain('Online');
    }
    // Retry is offered where it can help, and not on a host that was removed.
    const offered = feed.kind === 'failed' && feed.reason !== 'closed';
    expect(all(cardOf(shell, 'This Mac'), {role: 'button', name: 'Retry'}), word).toHaveLength(
      offered ? 1 : 0,
    );
  }
});

test('a failed host says why in its status, and Retry is answered with the last values, dimmed, until the stream returns', async () => {
  const shell = await open('a failed machine');
  const mac = () => cardOf(shell, 'This Mac');
  expect(chips(shell, 'This Mac')).toEqual(['Unreachable']);
  expect(gauges(shell, 'This Mac')).toHaveLength(0);
  // A real button, named for its machine, with the visible text `Retry`.
  expect(textOf(one(mac(), {role: 'button', name: 'Retry This Mac'}))).toBe('Retry');
  // The header status is the card's one status; nothing is a banner.
  expect(shell.container.querySelectorAll('[role="alert"]')).toHaveLength(0);
  await press(one(mac(), {role: 'button', name: 'Retry This Mac'}));
  await until('the retry heard', () => hosts().retried().length > 0);
  expect(hosts().retried()).toEqual(['local']);
  await chipsAre(shell, 'This Mac', ['Connecting']);
  expect(dimmed(shell, 'This Mac')).toBe(2);
  expect(textOf(mac())).toMatch(/\d+ (s|min) ago/);
  expect(all(mac(), {role: 'button', name: /Retry/})).toHaveLength(0);
  await set('local', {kind: 'live', readings: BUSY});
  await chipsAre(shell, 'This Mac', ['Online']);
  expect(dimmed(shell, 'This Mac')).toBe(0);
  // Failed again: the host still does not answer, and Retry is there to be pressed once more.
  await set('local', {kind: 'failed', reason: 'unreachable', retrying: true});
  await chipsAre(shell, 'This Mac', ['Unreachable']);
  await press(one(mac(), {role: 'button', name: 'Retry This Mac'}));
  await chipsAre(shell, 'This Mac', ['Connecting']);
  await set('local', {kind: 'failed', reason: 'unreachable', retrying: true});
  await chipsAre(shell, 'This Mac', ['Unreachable']);
  one(mac(), {role: 'button', name: 'Retry This Mac'});
});

test('each failed host has a Retry named for it, and pressing one asks only that host', async () => {
  const shell = await open('four machines in every state');
  expect(textOf(one(cardOf(shell, 'host-b'), {role: 'button', name: 'Retry host-b'}))).toBe(
    'Retry',
  );
  expect(all(shell.container, {role: 'button', name: /^Retry/})).toHaveLength(1);
  await press(one(cardOf(shell, 'host-b'), {role: 'button', name: 'Retry host-b'}));
  await until('the retry heard', () => hosts().retried().length > 0);
  expect(hosts().retried()).toEqual(['ssh:host-b']);
});

test('the page watches a host only while it is on screen', async () => {
  const shell = await open('a live machine');
  await until('the host watched', () => hosts().observing('local') === 1);
  await press(one(rail(shell), {role: 'button', name: 'Runs', exact: true}));
  await shell.read();
  await until('the host left alone', () => hosts().observing('local') === 0);
  await press(one(rail(shell), {role: 'button', name: 'Machines', exact: true}));
  await shell.read();
  await until('the host watched again', () => hosts().observing('local') === 1);
  await until('the gauges back', () => gauges(shell, 'This Mac').length === 2);
});

test('a host that streams is one status: a check that timed out says nothing of it, on the card or the rail', async () => {
  const shell = await open('a loaded machine');
  expect(chips(shell, 'This Mac')).toEqual(['Online']);
  expect(all(cardOf(shell, 'This Mac'), {role: 'button', name: 'Reconnect'})).toHaveLength(0);
  expect(gauges(shell, 'This Mac')).toEqual(['CPU34%', 'Memory18.0 / 36.0 GB']);
  one(rail(shell), {role: 'button', name: /^Machines$/});
  // When the host stops delivering, the check's word and its fix are all there is to go on.
  await set('local', {kind: 'failed', reason: 'unreachable', retrying: true});
  await chipsAre(shell, 'This Mac', ['Unreachable']);
  one(cardOf(shell, 'This Mac'), {role: 'button', name: 'Reconnect'});
  await until('the rail to say so', () => warning(shell) !== null);
  expect(maybe(rail(shell), {role: 'button', name: /^Machines, Unreachable$/})).not.toBeNull();
});
