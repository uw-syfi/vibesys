import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {
  fixtureHomeApi,
  type HomeApi,
  type HomeRun,
  homeRun,
  httpHomeApi,
  loadListing,
  openRun,
  relativeTime,
  sidebarSections,
} from './home.js';
import {type Gateway, homeClient, type RunRow} from './home-api.js';
import {runHref} from './route.js';

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
  loop: 'agent',
  budget: 12,
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

const WS = 'ws://127.0.0.1:53211/ws?token=gw';
const gw = (state: Gateway['state'], patch: Partial<Gateway> = {}): Gateway => ({
  state,
  url: null,
  websocket_url: null,
  token: null,
  stderr_tail: [],
  stderr_log: null,
  origin_mismatch: false,
  ...patch,
});
const row = (patch: Partial<RunRow>): RunRow => ({
  run_id: 'llm-serve-20260925-140100',
  loop: 'agent',
  status: 'completed',
  rounds: 12,
  gateway: gw('none'),
  reopen: null,
  budget: 12,
  error: null,
  task: 'decode',
  objective: 'Increase decode throughput\n\nWithout changing outputs.',
  created_at: '2026-09-25T14:01:00+00:00',
  ...patch,
});

test('home runs: serving gateways open directly, finished runs reopen, launches in flight wait', () => {
  const live = homeRun(
    row({status: 'active', rounds: 1, gateway: gw('live', {websocket_url: WS})}),
    'p1',
    'h',
  );
  assert.deepEqual(live, {
    id: 'llm-serve-20260925-140100',
    projectId: 'p1',
    title: 'Increase decode throughput',
    gateway: 'live',
    outcome: 'running',
    updatedAt: '2026-09-25T14:01:00+00:00',
    url: runHref('h', 'p1', WS),
    loop: 'agent',
    budget: 12,
  });
  const finished = homeRun(row({}), 'p1', 'h');
  assert.deepEqual(
    [finished.title, finished.outcome, finished.url],
    ['Increase decode throughput', 'completed', '/?token=h#open=p1/llm-serve-20260925-140100'],
  );
  assert.equal(
    homeRun(row({reopen: gw('reopened', {websocket_url: WS})}), 'p1', 'h').url,
    runHref('h', 'p1', WS),
  );
  assert.equal(homeRun(row({status: 'active', gateway: gw('starting')}), 'p1', 'h').url, null);
  assert.equal(homeRun(row({status: 'failed', gateway: gw('failed')}), 'p1', 'h').url, null);
  const stranded = gw('live', {websocket_url: WS, origin_mismatch: true});
  assert.equal(homeRun(row({status: 'active', gateway: stranded}), 'p1', 'h').url, null);
  const external = gw('external', {url: 'http://127.0.0.1:8765/?token=x', websocket_url: WS});
  assert.equal(
    homeRun(row({status: 'active', gateway: external}), 'p1', 'h').url,
    'http://127.0.0.1:8765/?token=x',
  );
  assert.equal(homeRun(row({objective: null}), 'p1', 'h').title, 'decode');
  assert.equal(homeRun(row({objective: '  ', task: null}), 'p1', 'h').title, 'Agent run');
  const bare = homeRun(row({loop: null, task: null, objective: null, created_at: null}), 'p1', 'h');
  assert.deepEqual([bare.title, bare.updatedAt], ['Run', '']);
});

test('one failing project drops only its own runs', async () => {
  const kept: HomeRun = {...other, projectId: 'p1'};
  const home: HomeApi = {
    projects: async () => [
      {id: 'p1', name: 'llm-serve', path: '/a'},
      {id: 'p2', name: 'gone', path: '/b'},
    ],
    runs: async projectId => {
      if (projectId === 'p2') throw new Error('unknown_project');
      return [kept];
    },
  };
  const listing = await loadListing(home);
  assert.deepEqual(listing.runs, [kept]);
  assert.equal(listing.projects.length, 2);
  const down: HomeApi = {
    projects: async () => {
      throw new Error('down');
    },
    runs: async () => [],
  };
  assert.deepEqual(await loadListing(down), {projects: [], runs: []});
  assert.deepEqual(await loadListing(fixtureHomeApi()), {projects: [], runs: []});
  // A refresh keeps what it last had: all of it when the server is down, a failing project's runs.
  assert.deepEqual(await loadListing(down, listing), listing);
  const gone: HomeRun = {...other, id: 'g', projectId: 'p2'};
  const refreshed = await loadListing(home, {...listing, runs: [kept, gone]});
  assert.deepEqual(refreshed.runs, [kept, gone]);
});

test('HTTP listings skip missing folders and resume polling when each folder returns', async () => {
  const projects = [
    {id: 'p1', name: 'first', root: '/a', last_opened: '', missing: false},
    {id: 'p2', name: 'second', root: '/b', last_opened: '', missing: false},
  ];
  const requests: string[] = [];
  const client = homeClient('home-token', async url => {
    requests.push(url);
    if (url === '/api/projects') return Response.json({projects, home: null});
    const project = projects.find(item => url === `/api/projects/${item.id}/runs`);
    assert.ok(project !== undefined);
    return project.missing
      ? Response.json({error: {code: 'unknown_project', message: 'missing'}}, {status: 404})
      : Response.json({runs: [row({run_id: project.id})]});
  });
  const home = httpHomeApi(client, 'home-token');
  let previous = await loadListing(home);
  // Every availability combination, including both transition directions and reordered projects.
  for (const mask of [1, 3, 2, 0, 2, 1, 0]) {
    for (const project of projects) {
      project.missing = Boolean(mask & (project.id === 'p1' ? 1 : 2));
    }
    projects.reverse();
    requests.length = 0;
    const listing = await loadListing(home, previous);
    const available = projects.filter(project => !project.missing);
    assert.deepEqual(requests, [
      '/api/projects',
      ...available.map(project => `/api/projects/${project.id}/runs`),
    ]);
    assert.deepEqual(
      listing.projects.map(project => project.missing),
      projects.map(p => p.missing),
    );
    assert.deepEqual(
      listing.runs.map(run => run.projectId),
      available.map(project => project.id),
    );
    assert.equal(sidebarSections(listing.projects, listing.runs, null).length, 2);
    previous = listing;
  }
  const failed: HomeApi = {
    projects: async () => [
      {id: 'p1', name: 'first', path: '/a', missing: true},
      {id: 'p2', name: 'second', path: '/b', missing: false},
    ],
    runs: async () => {
      throw new Error('temporarily unavailable');
    },
  };
  assert.deepEqual(
    (await loadListing(failed, previous)).runs,
    previous.runs.filter(run => run.projectId === 'p2'),
  );
});
