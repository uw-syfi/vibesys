import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {fixtureHomeApi, type HomeRun, openRun, relativeTime, sidebarSections} from './home.js';

const open = openRun({
  runId: 'r1',
  title: 'Increase decode throughput',
  project: 'llm-serve',
  status: 'running',
  updatedAt: '2026-09-25T14:01:00Z',
});
const other: HomeRun = {
  id: 'r0',
  projectId: 'p1',
  title: 'Reduce p99 prefill latency',
  gateway: 'none',
  outcome: 'completed',
  updatedAt: '2026-09-23T14:00:00Z',
  url: null,
};

test('the open run is listed even when the home API knows nothing', () => {
  assert.equal(open?.run.gateway, 'live');
  assert.equal(open?.run.outcome, 'running');
  assert.deepEqual(sidebarSections([], [], open), [
    {id: 'llm-serve', name: 'llm-serve', runs: [open?.run]},
  ]);
  assert.deepEqual(sidebarSections([], [], null), []);
});

test('the open run joins its project first, or replaces its own stale row in place', () => {
  const projects = [{id: 'p1', name: 'llm-serve', path: '/home/demo/llm-serve'}];
  assert.deepEqual(
    sidebarSections(projects, [other], open)[0]?.runs.map(run => run.id),
    ['r1', 'r0'],
  );
  const stale: HomeRun = {...other, id: 'r1', title: 'old title'};
  const [section] = sidebarSections(projects, [other, stale], open);
  assert.deepEqual(
    section?.runs.map(run => run.title),
    ['Reduce p99 prefill latency', 'Increase decode throughput'],
  );
});

test('relative time: now, minutes, hours, days, weeks', () => {
  const now = new Date('2026-09-25T14:00:00Z');
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString();
  assert.deepEqual(
    [30, 300, 3 * 3600, 2 * 86400, 9 * 86400].map(seconds => relativeTime(ago(seconds), now)),
    ['now', '5m', '3h', '2d', '1w'],
  );
  assert.equal(relativeTime('', now), 'now');
});

test('the fixture answers runs per project', async () => {
  const api = fixtureHomeApi([{id: 'p1', name: 'llm-serve', path: '/x'}], [other]);
  assert.deepEqual(
    (await api.runs('p1')).map(run => run.id),
    ['r0'],
  );
  assert.deepEqual(await api.runs('p2'), []);
});
