import {afterAll, beforeAll, expect, test} from 'bun:test';
import type {HostKey, HostRun} from '@vibesys/hosts';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {createRunReader, RUN_ROUTE, runPageInitial, runPageStep} from '../../state/index.js';
import {RunScreen} from './run-screen.js';

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
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

test('property: run-level spacing creates no inert or unnamed button on any face', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.constantFrom('ideas', 'timeline', 'agents', 'usage', 'logs'),
      fc.constantFrom('starting', 'running', 'pausing', 'paused'),
      async (mode, status) => {
        const run: HostRun = {
          id: 'instance-a',
          runId: 'run-a',
          projectRoot: '/project',
          startedAt: 1,
          status,
          phase: status,
          paused: status === 'paused' ? 'user' : null,
          end: null,
          reason: null,
        };
        let state = runPageStep(
          runPageInitial({
            identity: {host: 'local' as HostKey, runId: 'run-a'},
            title: {project: '/project', task: 'mpmc'},
            hostLabel: 'host-a',
            unavailable: {},
          }),
          {type: 'record', record: {kind: 'live', run}},
        ).state;
        if (state.link.kind !== 'attaching') throw new Error('Attach required');
        state = runPageStep(state, {type: 'attached', token: state.link.token, error: null}).state;
        state = runPageStep(state, {
          type: 'session',
          session: {hasSnapshot: true, streaming: true, controls: true, error: null},
        }).state;
        const model = createRunReader().read(state.run.trace, null);
        const container = document.createElement('div');
        const root = createRoot(container);
        let left = 0;
        try {
          await act(async () =>
            root.render(
              <RunScreen
                state={state}
                model={model}
                whole={model}
                core={null}
                route={{...RUN_ROUTE, mode}}
                zoom={() => undefined}
                dispatch={() => undefined}
                runs={() => {
                  left += 1;
                }}
              />,
            ),
          );
          const crumbs = container.querySelector('nav[aria-label="Zoom"]');
          const runs = [...(crumbs?.querySelectorAll('button') ?? [])].filter(
            button => button.textContent === 'Runs',
          );
          expect(runs).toHaveLength(1);
          await act(async () => runs[0]?.click());
          expect(left).toBe(1);
          // A window that shows only this run has nowhere up to go.
          await act(async () =>
            root.render(
              <RunScreen
                state={state}
                model={model}
                whole={model}
                core={null}
                route={{...RUN_ROUTE, mode}}
                zoom={() => undefined}
                dispatch={() => undefined}
              />,
            ),
          );
          expect(
            [...container.querySelectorAll('nav[aria-label="Zoom"] button')].filter(
              button => button.textContent === 'Runs',
            ),
          ).toEqual([]);
          expect(container.querySelector('[inert] button') === null).toBe(true);
          for (const button of container.querySelectorAll('button'))
            expect((button.getAttribute('aria-label') ?? button.textContent ?? '').trim()).not.toBe(
              '',
            );
        } finally {
          await act(async () => root.unmount());
        }
      },
    ),
    {numRuns: 20, seed: 3430},
  );
});
