import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import {renderToStaticMarkup} from 'react-dom/server';
import {COMPLETION_EMPTY, hostInitial, hostStep} from '../../state/index.js';
import {HostPanel} from '../index.js';

test('live runs do not hide unreadable and unverified registry verdicts', () => {
  const loading = hostStep(hostInitial('local'), {type: 'show', host: 'local'});
  const request = loading.requests[0];
  if (request === undefined || request.kind !== 'host') throw new Error('host request expected');
  const state = hostStep(loading.state, {
    type: 'host-reply',
    token: request.token,
    result: {ok: true, value: {key: 'local', label: 'This Mac', checkout: '/src/vibesys'}},
  }).state;
  const listing: HostListing = {
    kind: 'listed',
    host: 'local',
    live: [
      {
        id: 'live00000001',
        projectRoot: '/project',
        runId: 'run-live',
        status: 'running',
        phase: 'running',
        paused: null,
        end: null,
        startedAt: 1,
        reason: null,
      },
    ],
    ended: [],
    unreadable: ['unreadable01'],
    unverified: ['unverified02'],
  };

  const html = renderToStaticMarkup(
    <HostPanel
      state={state}
      listing={listing}
      completion={COMPLETION_EMPTY}
      actions={{attach: () => undefined, stopControls: () => null, inspect: () => undefined}}
      send={{host: () => undefined, completion: () => undefined, startRun: () => undefined}}
    />,
  );

  expect(html).toContain('run-live');
  expect(html).toContain('1 run(s) on This Mac belong to another VibeSys version.');
  expect(html).toContain('1 run(s) on This Mac could not be confirmed');
  expect(html).toContain('their lock could not be read');
});

test('ended runs render as tombstones, with their status and failure reason', () => {
  const loading = hostStep(hostInitial('local'), {type: 'show', host: 'local'});
  const request = loading.requests[0];
  if (request === undefined || request.kind !== 'host') throw new Error('host request expected');
  const state = hostStep(loading.state, {
    type: 'host-reply',
    token: request.token,
    result: {ok: true, value: {key: 'local', label: 'This Mac', checkout: '/src/vibesys'}},
  }).state;
  const listing: HostListing = {
    kind: 'listed',
    host: 'local',
    live: [],
    ended: [
      {
        id: 'ended00000001',
        projectRoot: '/project',
        runId: 'run-failed',
        status: 'failed',
        phase: 'failed',
        paused: null,
        end: null,
        startedAt: 1,
        reason: 'docker run --gpus all: no GPU available',
      },
      {
        id: 'ended00000002',
        projectRoot: '/project-2',
        runId: 'run-finished',
        status: 'finished',
        phase: 'completed',
        paused: null,
        end: null,
        startedAt: 2,
        reason: null,
      },
    ],
    unreadable: [],
    unverified: [],
  };

  const html = renderToStaticMarkup(
    <HostPanel
      state={state}
      listing={listing}
      completion={COMPLETION_EMPTY}
      actions={{attach: () => undefined, stopControls: () => null, inspect: () => undefined}}
      send={{host: () => undefined, completion: () => undefined, startRun: () => undefined}}
    />,
  );

  expect(html).toContain('run-failed');
  expect(html).toContain('/project');
  expect(html).toContain('failed: docker run --gpus all: no GPU available');
  expect(html).toContain('run-finished');
  expect(html).toContain('/project-2');
  expect(html).toContain('finished');
});

test('an ended run offers Inspect and no live actions', () => {
  const loading = hostStep(hostInitial('local'), {type: 'show', host: 'local'});
  const request = loading.requests[0];
  if (request === undefined || request.kind !== 'host') throw new Error('host request expected');
  const state = hostStep(loading.state, {
    type: 'host-reply',
    token: request.token,
    result: {ok: true, value: {key: 'local', label: 'This Mac', checkout: '/src/vibesys'}},
  }).state;
  const ended = {
    id: '000000000009',
    projectRoot: '/project',
    runId: 'run-ended',
    status: 'failed',
    phase: 'failed',
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  } as const;
  const listing: HostListing = {
    kind: 'listed',
    host: 'local',
    live: [],
    ended: [ended],
    unreadable: [],
    unverified: [],
  };
  const html = renderToStaticMarkup(
    <HostPanel
      state={state}
      listing={listing}
      completion={COMPLETION_EMPTY}
      actions={{attach: () => undefined, stopControls: () => null, inspect: () => undefined}}
      send={{host: () => undefined, completion: () => undefined, startRun: () => undefined}}
    />,
  );

  expect(html).toContain('run-ended');
  expect(html.match(/>Inspect</g)).toHaveLength(1);
  expect(html).not.toContain('>Attach<');
});
