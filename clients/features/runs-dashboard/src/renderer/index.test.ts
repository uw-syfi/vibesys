import {expect, test} from 'bun:test';
import {createGroupingStore} from './index.js';

test('the grouping store starts on status, follows a choice, and tells its subscribers once', () => {
  const store = createGroupingStore();
  expect(store.getState()).toBe('status');
  let heard = 0;
  store.subscribe(() => {
    heard += 1;
  });
  store.dispatch({type: 'group', by: 'machine'});
  store.dispatch({type: 'group', by: 'machine'});
  expect(store.getState()).toBe('machine');
  expect(heard).toBe(1);
});
