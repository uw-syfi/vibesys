import {expect, test} from 'bun:test';
import {createGroupingStore} from './index.js';

test('the grouping store starts on status for every machine, follows a choice, and tells its subscribers once', () => {
  const store = createGroupingStore();
  expect(store.getState()).toEqual({by: 'status', machine: null});
  let heard = 0;
  store.subscribe(() => {
    heard += 1;
  });
  store.dispatch({type: 'group', by: 'machine'});
  store.dispatch({type: 'group', by: 'machine'});
  expect(store.getState().by).toBe('machine');
  expect(heard).toBe(1);
});

test('narrowing to a machine, and removing it, each tell the subscribers once', () => {
  const store = createGroupingStore();
  let heard = 0;
  store.subscribe(() => {
    heard += 1;
  });
  store.dispatch({type: 'filter-machine', machine: 'host-a'});
  store.dispatch({type: 'filter-machine', machine: 'host-a'});
  expect(store.getState().machine).toBe('host-a');
  store.dispatch({type: 'clear-machine'});
  store.dispatch({type: 'clear-machine'});
  expect(store.getState().machine).toBeNull();
  expect(heard).toBe(2);
});
