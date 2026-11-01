/**
 * What the run's chat says and does: it replays the session's history, posts an explicit steer and
 * a question to the host, shows the answer as it streams in, says when its history is unavailable
 * and reads it again on Retry, and holds no stream once it is closed and the run left. A parked or
 * stopped run keeps its chat history readable. The shell is drawn in a DOM over the journeys' Fake
 * gateway; where each of these sits is the browser's (`e2e/run-sessions.spec.ts`).
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {RecordSchema} from '@vibesys/proto';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {fillIn, press} from './testing/dom-controls.js';
import {all, maybe, notSee, one, see, until} from './testing/dom-query.js';
import {shellOnRun} from './testing/run-screen.js';
import {chatTail, exchange, sessionWorld} from './testing/sessions-world.js';
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
  await shell?.close();
  shell = undefined;
  Reflect.deleteProperty(window, 'postIntent');
});

const HISTORY = 'The queue avoids repeated allocation.';

async function opened(state: 'live' | 'parked' | 'stopped') {
  const world = sessionWorld(state);
  Object.assign(window, {
    postIntent: (host: string, bytes: number[]) => Promise.resolve(world.postIntent(host, bytes)),
  });
  shell = await shellOnRun(
    {input: world.input, updates: {}},
    new FakeClock().schedule,
    state === 'live'
      ? {...fixtureNamed('queue-rs-live'), runId: 'run-a'}
      : {...fixtureNamed('finished-run'), runId: 'run-a'},
  );
  const {container} = shell;
  await press(one(container, {role: 'button', name: 'Chat', exact: true}));
  const pane = all(container, {role: 'region', name: 'Chat', exact: true}).at(-1) as HTMLElement;
  await see(pane, HISTORY, true);
  return {world, shell, container, pane};
}

test('a live run replays its chat, posts a steer and a question, streams the answer, and recovers its history', async () => {
  const {world, shell, container, pane} = await opened('live');
  expect(world.requests).toHaveLength(0);
  await press(one(pane, {role: 'button', name: 'Send', exact: true}));
  await until('the steer posted', () => world.requests.length === 1);
  await see(pane, 'Sent', true);
  expect(world.requests[0]?.method).toMatchObject({
    case: 'postIntent',
    value: {
      intent: {
        case: 'steer',
        value: {text: 'Compare allocation counts before changing the queue.'},
      },
    },
  });
  await fillIn(
    one(pane, {role: 'textbox', name: 'Message'}) as HTMLTextAreaElement,
    'Why did it win?',
  );
  await press(one(pane, {role: 'button', name: 'Ask', exact: true}));
  await until('the question posted', () => world.requests.length === 2);
  const method = world.requests[1]?.method;
  if (method?.case !== 'chatMessage') throw new Error('Expected chat-message');
  const {question, token, answer} = exchange(method.value.requestId, method.value.text);
  await act(async () => shell.gateway?.appendChat(chatTail([question, token])));
  await see(pane, 'It reduces contention', true);
  await act(async () => shell.gateway?.appendChat(chatTail([answer])));
  await see(pane, 'It reduces contention under load.', true);
  // A frame past a gap in the history makes it unavailable until it is read again.
  await act(async () =>
    shell.gateway?.sendChatFrame(chatTail([create(RecordSchema, {...answer, seq: 10n})])),
  );
  await see(pane, 'Unavailable', true);
  await press(one(pane, {role: 'button', name: 'Retry history'}));
  await until(
    'the history read again',
    () => maybe(pane, {role: 'button', name: 'Retry history'}) === null,
  );
  notSee(pane, 'Unavailable', true);
  await see(pane, 'It reduces contention under load.', true);
  await press(one(pane, {role: 'button', name: 'Close', exact: true}));
  await until(
    'the chat closed',
    () => all(container, {role: 'region', name: 'Chat', exact: true}).length === 0,
  );
  await press(one(container, {role: 'button', name: 'Settings', exact: true}));
  // Leaving the run releases every stream it held, the chat's included.
  expect(shell.gateway?.active()).toBe(0);
});

for (const state of ['parked', 'stopped'] as const) {
  test(`a ${state} run retains its chat history, and offers Resume once the chat is closed`, async () => {
    const {world, container, pane} = await opened(state);
    expect(world.requests).toHaveLength(0);
    await press(one(pane, {role: 'button', name: 'Close', exact: true}));
    one(container, {role: 'button', name: 'Resume', exact: true});
  });
}
