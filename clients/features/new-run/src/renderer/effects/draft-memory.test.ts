import {expect, test} from 'bun:test';
import {DRAFT_KEY, NO_ANSWERS} from '../../state/index.js';
import {browserDraftMemory} from './draft-memory.js';

const DRAFT = {
  machine: 'local',
  answers: {...NO_ANSWERS, from: 'example' as const},
  at: 'pick' as const,
  agent: null,
  budget: {rounds: null, timeLimitS: null, results: 'keep' as const},
  measure: {kind: 'same_machine' as const},
  advanced: '',
  destination: null,
};

function storage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: key => items.get(key) ?? null,
    key: index => [...items.keys()][index] ?? null,
    removeItem: key => void items.delete(key),
    setItem: (key, value) => void items.set(key, value),
  };
}

test('a draft is stored under one key, read back, and forgotten by writing none', () => {
  const store = storage();
  const memory = browserDraftMemory(() => store);
  expect(memory.read()).toBeNull();
  memory.write(DRAFT);
  expect(store.getItem(DRAFT_KEY)).not.toBeNull();
  expect(memory.read()).toEqual(DRAFT);
  memory.write(null);
  expect(store.getItem(DRAFT_KEY)).toBeNull();
});

test('storage that is missing, throws or holds something else keeps no draft and breaks nothing', () => {
  expect(browserDraftMemory(() => null).read()).toBeNull();
  browserDraftMemory(() => null).write(DRAFT);
  const throwing = () => {
    throw new Error('blocked');
  };
  expect(browserDraftMemory(throwing).read()).toBeNull();
  browserDraftMemory(throwing).write(DRAFT);
  const store = storage();
  store.setItem(DRAFT_KEY, '{"machine":"local"}');
  expect(browserDraftMemory(() => store).read()).toBeNull();
});
