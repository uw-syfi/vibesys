import {describe, expect, test} from 'bun:test';
import {registryProjects} from '@vibesys/connection';
import {olderHost} from '@vibesys/connection/testing';
import type {HostId} from '@vibesys/hosts';
import type {StoredRecentRun} from '@vibesys/settings';
import {NewRunMain} from './index.js';
import {NATIVE_FEATURES} from './native-features.js';

const LOCAL = {kind: 'local'} as const;

describe('NewRunMain', () => {
  test('a start uses the registry', async () => {
    const remembered: StoredRecentRun[] = [];
    const watched: HostId[] = [];
    const main = new NewRunMain({
      remember: async run => {
        remembered.push(run);
      },
      now: () => 42,
      registryStart: async () => ({runId: 'run-9', instanceId: 'inst-9'}),
      watchRuns: {
        start(host) {
          watched.push(host);
        },
      },
    });
    const started = await main.answer({
      kind: 'start-run',
      host: LOCAL,
      inputs: {
        project: '/p',
        task: 'mpmc',
        harness: null,
        options: {},
        agents: {},
        measure_on: {kind: 'same_machine'},
        args: [],
      },
    });
    expect(started).toEqual({runId: 'run-9', instanceId: 'inst-9'});
    expect(watched).toEqual([LOCAL]);
    expect(remembered[0]?.runId).toBe('run-9');
  });

  test('create, copy and destination use the registry', async () => {
    const calls: string[] = [];
    const main = new NewRunMain({
      remember: async () => {},
      now: () => 1,
      registryProjects: {
        destination: async () => {
          calls.push('destination');
          return {state: 'absent', project: null};
        },
        create: async () => {
          calls.push('create');
          return {kind: 'created', created: {project: '/work/queue', tasks: ['a']}};
        },
        copy: async () => {
          calls.push('copy');
          return {project: '/own', tasks: ['a']};
        },
      },
    });
    expect(
      await main.answer({
        kind: 'example-destination',
        host: LOCAL,
        example: 'queue',
        destination: '/work/queue',
      }),
    ).toEqual({kind: 'offered', value: {state: 'absent', project: null}});
    expect(
      await main.answer({
        kind: 'create-from-example',
        host: LOCAL,
        example: 'queue',
        destination: '/work/queue',
      }),
    ).toEqual({kind: 'created', created: {project: '/work/queue', tasks: ['a']}});
    expect(
      await main.answer({
        kind: 'copy-out',
        host: LOCAL,
        source: '/nested',
        destination: '/own',
      }),
    ).toEqual({project: '/own', tasks: ['a']});
    expect(calls).toEqual(['destination', 'create', 'copy']);
  });

  test('a host whose VibeSys predates the registry needs an update on the destination check and on creating', async () => {
    const main = new NewRunMain({
      remember: async () => {},
      now: () => 1,
      registryProjects: registryProjects({
        checkout: async () => '/checkout',
        read: olderHost.read,
        mutate: olderHost.mutate,
      }),
    });
    expect(
      await main.answer({
        kind: 'example-destination',
        host: LOCAL,
        example: 'queue',
        destination: '/work/queue',
      }),
    ).toEqual({kind: 'unsupported'});
    await expect(
      main.answer({
        kind: 'create-from-example',
        host: LOCAL,
        example: 'queue',
        destination: '/work/queue',
      }),
    ).rejects.toThrow("This host's VibeSys needs an update");
  });

  test('answers every feature the screens ask about, with no helper to negotiate with', async () => {
    const main = new NewRunMain({remember: async () => {}, now: () => 1});
    const features = (await main.answer({kind: 'helper-features', host: LOCAL})) as string[];
    expect(features).toEqual([...NATIVE_FEATURES]);
    expect(features).toEqual(expect.arrayContaining(['preflight', 'run_options', 'examples']));
  });
});
