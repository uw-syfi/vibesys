import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {agentFilter, forRun, frame, INITIAL_UI, PANE, SIDE, uiReducer} from './ui-state.js';

test('picking the live round follows it again; another round pins and clears row state', () => {
  const busy = {...INITIAL_UI, expanded: '26', agent: 'x1', agentRound: 6};
  const pinned = uiReducer(busy, {type: 'round', round: 3, live: 6});
  assert.deepEqual([pinned.round, pinned.expanded, pinned.agent], [3, null, null]);
  assert.equal(uiReducer(pinned, {type: 'round', round: 6, live: 6}).round, 'live');
});

test('toggles: tool rows, disclosures (or set open), the agent filter, evidence', () => {
  let state = uiReducer(INITIAL_UI, {type: 'expand', id: '26'});
  assert.equal(state.expanded, '26');
  state = uiReducer(state, {type: 'expand', id: '26'});
  assert.equal(state.expanded, null);
  state = uiReducer(state, {type: 'disclose', key: 'x1:prompt'});
  assert.equal(state.disclosed['x1:prompt'], true);
  state = uiReducer(state, {type: 'disclose', key: 'x1:prompt', open: true});
  assert.equal(state.disclosed['x1:prompt'], true);
  state = uiReducer(state, {type: 'agent', id: 'x1', round: 6});
  assert.equal(uiReducer(state, {type: 'agent', id: 'x1', round: 6}).agent, null);
  assert.equal(uiReducer(state, {type: 'agent', id: null, round: 6}).agent, null);
  state = uiReducer(state, {type: 'evidence', round: 3});
  assert.equal(uiReducer(state, {type: 'evidence', round: 3}).evidence, null);
});

test('the agent filter holds only in the round it was set in', () => {
  const filtered = uiReducer(INITIAL_UI, {type: 'agent', id: 'x1', round: 6});
  assert.equal(agentFilter(filtered, 6), 'x1');
  // The followed live round advances: nothing is dispatched, and the filter lapses.
  assert.equal(agentFilter(filtered, 7), null);
  const picked = uiReducer(filtered, {type: 'round', round: 3, live: 6});
  assert.deepEqual([picked.agent, agentFilter(picked, 6)], [null, null]);
});

test('the pane opens on Changes and choosing a tab closes a menu', () => {
  const opened = uiReducer({...INITIAL_UI, menu: 'more'}, {type: 'togglePane'});
  assert.equal(opened.pane, 'changes');
  assert.deepEqual(uiReducer(opened, {type: 'pane', pane: 'notes'}), {
    ...opened,
    pane: 'notes',
    menu: null,
  });
  assert.equal(uiReducer(opened, {type: 'togglePane'}).pane, null);
});

test('widths clamp to the mockup ranges', () => {
  assert.equal(
    uiReducer(INITIAL_UI, {type: 'resize', target: 'side', width: 50}).sideWidth,
    SIDE.min,
  );
  assert.equal(
    uiReducer(INITIAL_UI, {type: 'resize', target: 'pane', width: 5000}).paneWidth,
    PANE.max,
  );
});

test('selection belongs to one run: a replaced run starts clean and keeps the layout', () => {
  const busy = {
    ...INITIAL_UI,
    runId: 'run-1',
    round: 3,
    expanded: '26',
    disclosed: {'x1:prompt': true},
    agent: 'x1',
    evidence: 3,
    palette: true,
    pane: 'agents' as const,
    sideWidth: 300,
  };
  assert.equal(forRun(busy, 'run-1'), busy);
  const next = forRun(busy, 'run-2');
  assert.deepEqual(
    [
      next.runId,
      next.round,
      next.expanded,
      next.disclosed,
      next.agent,
      next.evidence,
      next.palette,
    ],
    ['run-2', 'live', null, {}, null, null, false],
  );
  assert.deepEqual([next.pane, next.sideWidth], ['agents', 300]);
  assert.deepEqual(uiReducer(busy, {type: 'run', runId: 'run-2'}), next);
});

test('drafts and the Ask thread belong to one run', () => {
  let state = forRun(INITIAL_UI, 'run-1');
  state = uiReducer(state, {type: 'draft', target: 'steer', text: 'Measure first.'});
  state = uiReducer(state, {type: 'draft', target: 'ask', text: 'Why?'});
  state = uiReducer({...state, menu: 'thread'}, {type: 'thread', id: 't2'});
  assert.deepEqual(
    [state.drafts, state.thread, state.menu],
    [{steer: 'Measure first.', ask: 'Why?'}, 't2', null],
  );
  const created = uiReducer(state, {
    type: 'thread',
    id: 't3',
    model: {provider: 'claude', model: 'sonnet'},
  });
  assert.deepEqual(created.threadModel, {provider: 'claude', model: 'sonnet'});
  assert.equal(uiReducer(created, {type: 'thread', id: 't2'}).threadModel, null);
  const next = forRun(created, 'run-2');
  assert.deepEqual(
    [next.drafts, next.thread, next.threadModel],
    [{steer: '', ask: ''}, 'default', null],
  );
});

test('the sidebar yields before the transcript drops below 560px', () => {
  const open = {...INITIAL_UI, pane: 'changes' as const};
  assert.deepEqual(frame(1440, open), {sidebar: true, paneWidth: 400});
  assert.deepEqual(frame(1024, open), {sidebar: false, paneWidth: 400});
  assert.deepEqual(frame(1024, INITIAL_UI), {sidebar: true, paneWidth: 0});
  assert.deepEqual(frame(900, {...open, paneWidth: 640}), {sidebar: false, paneWidth: 340});
  assert.deepEqual(frame(1440, {...open, sidebar: false}), {sidebar: false, paneWidth: 400});
});
