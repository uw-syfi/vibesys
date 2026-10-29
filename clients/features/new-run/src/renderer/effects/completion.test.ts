import {expect, test} from 'bun:test';
import {type PathCompletion, suggestions} from '@vibesys/hosts';
import {type CompletionBridge, createFolderCompletion} from './completion.js';

/** Listings answered when the test says, in any order. */
function fakeBridge() {
  const asked: {directory: string; answer: (value: PathCompletion) => void}[] = [];
  const bridge: CompletionBridge = {
    complete: (_host, directory) =>
      new Promise(resolve => {
        asked.push({directory, answer: value => resolve({ok: true, value})});
      }),
  };
  return {bridge, asked};
}

function listing(directory: string, names: readonly string[]): PathCompletion {
  return {
    kind: 'listed',
    directory,
    entries: names.map(name => ({
      name,
      path: `${directory}${name}`,
      checkout: false,
      project: true,
    })),
    truncated: false,
  };
}

/** A schedule whose pauses end when the test says; cancelling one drops it. */
function manualSchedule() {
  let due: (() => void)[] = [];
  return {
    schedule: (callback: () => void) => {
      due.push(callback);
      return () => {
        due = due.filter(each => each !== callback);
      };
    },
    runDue: () => {
      const now = due;
      due = [];
      for (const callback of now) callback();
    },
  };
}

async function settle(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

test('the folder field lists the typed directory after a pause and filters it as you type', async () => {
  const {bridge, asked} = fakeBridge();
  const clock = manualSchedule();
  const store = createFolderCompletion(bridge, clock.schedule);
  store.dispatch({type: 'input', field: 'project', host: 'ssh:gpu', value: '/home/me/'});
  expect(asked).toEqual([]);
  clock.runDue();
  expect(asked.map(each => each.directory)).toEqual(['/home/me/']);
  asked[0]?.answer(listing('/home/me/', ['queue-rs', 'kv-store']));
  await settle();
  expect(suggestions(store.getState(), 'project')).toEqual([
    '/home/me/queue-rs/',
    '/home/me/kv-store/',
  ]);
  store.dispatch({type: 'input', field: 'project', host: 'ssh:gpu', value: '/home/me/q'});
  expect(suggestions(store.getState(), 'project')).toEqual(['/home/me/queue-rs/']);
});

test('a directory left before its pause ends is never asked, and a late listing is dropped', async () => {
  const {bridge, asked} = fakeBridge();
  const clock = manualSchedule();
  const store = createFolderCompletion(bridge, clock.schedule);
  store.dispatch({type: 'input', field: 'project', host: 'local', value: '/a/'});
  store.dispatch({type: 'input', field: 'project', host: 'local', value: '/b/'});
  clock.runDue();
  expect(asked.map(each => each.directory)).toEqual(['/b/']);
  store.dispatch({type: 'input', field: 'project', host: 'local', value: '/c/'});
  clock.runDue();
  asked[1]?.answer(listing('/c/', ['now']));
  asked[0]?.answer(listing('/b/', ['stale']));
  await settle();
  expect(suggestions(store.getState(), 'project')).toEqual(['/c/now/']);
});
