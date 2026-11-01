/**
 * The run chat's panel on runs that carry chat threads, with no backend: on a run that ended it
 * opens on the run's session logs over the gateway and starts no run reader, shows a steer the
 * agent proposed as a card that cannot be sent, and offers the question box and New chat but no
 * New steer; a turn whose answer arrives in the terminal `chat` event draws without an error; New
 * chat opens an empty thread, and a thread with no title is never listed under that name. The shell
 * is drawn in a DOM over the journeys' Fake gateway; where the panel and its Threads menu sit at
 * each width is the browser's (`e2e/run-chat-threads.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {type ChatHistories, chatBatches, tailBytes} from './testing/chat-records.js';
import {
  ANSWER,
  content,
  created,
  proposal,
  QUESTION,
  sequenceAfter,
  turn,
} from './testing/chat-scenes.js';
import {press} from './testing/dom-controls.js';
import {
  all,
  byText,
  closed,
  maybe,
  notSee,
  one,
  see,
  settle,
  textOf,
  until,
} from './testing/dom-query.js';
import {fixtureWorld, shellOnRun} from './testing/run-screen.js';
import type {OpenShell} from './testing/shell-world.js';

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
  for (const name of [...installed, 'window', 'indexedDB', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

let shell: OpenShell | undefined;
afterEach(async () => {
  if (shell !== undefined) await settle();
  await shell?.close();
  shell = undefined;
});

interface FakeGatewayPeer {
  seedChat(id: string, bytes: number[]): void;
  calls(): readonly string[];
}
const gateway = (): FakeGatewayPeer =>
  Reflect.get(window, 'fakeGateway') as unknown as FakeGatewayPeer;

/** The run of `fixture`, its chat threads told by `events`, the panel not yet opened. */
async function opened(fixture: string, events: readonly RunEvent[]): Promise<HTMLElement> {
  const run = fixtureNamed(fixture);
  shell = await shellOnRun(fixtureWorld(run), new FakeClock().schedule, run);
  const sessions: ChatHistories = new Map();
  await act(async () => {
    for (const [id, batch] of chatBatches(sessions, events))
      if (batch.length > 0) gateway().seedChat(id, tailBytes(batch));
  });
  return shell.container;
}

const chatButton = (container: Element) =>
  one(container, {role: 'button', name: 'Chat', exact: true});
const panelOf = (container: Element) => one(container, {role: 'region', name: 'Chat'});

async function openChat(container: Element): Promise<HTMLElement> {
  await press(chatButton(container));
  await until('the chat panel', () => maybe(container, {role: 'region', name: 'Chat'}) !== null);
  await settle();
  return panelOf(container);
}

const ENDED = 'crashed-after-turn';
const LIVE = 'queue-rs-live';

test('Chat opens on a failed run through the gateway, and starts no run reader', async () => {
  const container = await opened(ENDED, []);
  expect(chatButton(container).hasAttribute('disabled')).toBe(false);
  const listed = () =>
    gateway()
      .calls()
      .filter(method => method.endsWith('/ListChatSessions')).length;
  const before = listed();
  await openChat(container);
  // The thread list is read again on opening; nothing is attached for the run, whose log is read.
  expect(listed()).toBeGreaterThan(before);
  expect(
    gateway()
      .calls()
      .filter(method => /attach|Reader/i.test(method)),
  ).toEqual([]);
});

test('a steer the agent proposed on an ended run cannot be sent', async () => {
  const container = await opened(
    ENDED,
    proposal(sequenceAfter(ENDED), 'thread-a', 'Why did it fail'),
  );
  const panel = await openChat(container);
  const steer = one(panel, {role: 'textbox', name: 'Steer text'}) as HTMLTextAreaElement;
  expect(steer.value).toBe('Fix the engine import before the next idea.');
  expect(closed(one(panel, {role: 'button', name: 'Send', exact: true}))).toBe(true);
  expect(byText(panel, 'Read only').length).toBeGreaterThan(0);
});

test('an ended run offers the question box and New chat, but no New steer', async () => {
  const container = await opened(
    ENDED,
    proposal(sequenceAfter(ENDED), 'thread-a', 'Why did it fail'),
  );
  const panel = await openChat(container);
  await see(panel, 'Why did it fail');
  expect(all(panel, {role: 'textbox', name: 'Message'})).toHaveLength(1);
  expect(all(panel, {role: 'button', name: 'Ask'})).toHaveLength(1);
  expect(all(panel, {role: 'button', name: 'New chat'})).toHaveLength(1);
  expect(all(panel, {role: 'button', name: 'New steer'})).toHaveLength(0);
});

test('Chat opens on a run whose thread holds a question and an answer, and settles', async () => {
  const container = await opened(LIVE, turn(sequenceAfter(LIVE), 'thread-a'));
  const panel = await openChat(container);
  await see(panel, QUESTION);
  await see(panel, ANSWER);
  notSee(panel, 'Chat failed');
  one(container, {role: 'heading', level: 1});
  // Reopening shows the same thread.
  await press(one(panel, {role: 'button', name: 'Close'}));
  await until('the panel closed', () => maybe(container, {role: 'region', name: 'Chat'}) === null);
  const again = await openChat(container);
  await see(again, ANSWER);
});

test('New chat opens an empty thread, with no transcript and nothing streaming', async () => {
  const container = await opened(
    LIVE,
    content(sequenceAfter(LIVE), 'thread-a', 'Best idea so far'),
  );
  const panel = await openChat(container);
  await see(panel, 'What is the best idea?');
  await see(panel, 'Writing', true);
  await press(one(panel, {role: 'button', name: 'New chat', exact: true}));
  await until(
    'the harness picker',
    () => maybe(panel, {role: 'combobox', name: 'Harness'}) !== null,
  );
  notSee(panel, 'What is the best idea?');
  notSee(panel, 'The lock-free ring is best.');
  notSee(panel, 'Writing', true);
  notSee(panel, 'Working', true);
});

test('a thread with no title is never listed as New chat', async () => {
  const from = sequenceAfter(LIVE);
  const container = await opened(LIVE, [
    ...content(from, 'thread-a', 'Best idea so far'),
    created(from + 3, 'thread-b', ''),
  ]);
  const panel = await openChat(container);
  await press(one(panel, {role: 'button', name: 'Threads'}));
  await until('the menu', () => all(container, {role: 'menuitem'}).length > 0);
  expect(all(container, {role: 'menuitem'}).map(textOf)).toEqual(['Untitled', 'Best idea so far']);
  expect(all(container, {role: 'menuitem', name: 'New chat'})).toHaveLength(0);
});
