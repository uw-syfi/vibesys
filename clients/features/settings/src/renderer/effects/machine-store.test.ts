import {expect, test} from 'bun:test';
import {type MachineSettingsWelcomeBridge, UNEXPECTED_FAILURE} from '@vibesys/ipc';
import {createMachineSettingsStore} from './machine-store.js';

async function settle(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

const COMPUTE = {
  config_path: '/home/me/.config/vibesys/compute.toml',
  clusters: [],
  modal: {token_present: false},
};

/** A bridge that answers every read, with `overrides` replacing calls. */
function bridge(
  overrides: Partial<MachineSettingsWelcomeBridge> = {},
): MachineSettingsWelcomeBridge {
  const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});
  return {
    compute: () => ok({kind: 'offered' as const, value: COMPUTE}),
    testCluster: () => ok({outcome: 'passed' as const, hardware: {}, partitions: []}),
    addCluster: () => ok(COMPUTE),
    removeCluster: () => ok(COMPUTE),
    agentLogins: () => ok({kind: 'unsupported' as const}),
    startLogin: () => ok({outcome: 'started' as const}),
    ...overrides,
  };
}

test('landing on a machine section reads that machine, once, and shows what it answered', async () => {
  const read: string[] = [];
  const store = createMachineSettingsStore(
    bridge({
      compute: host => {
        read.push(host);
        return Promise.resolve({ok: true, value: {kind: 'offered', value: COMPUTE}});
      },
    }),
  );
  store.dispatch({type: 'focus', host: 'ssh:host-b', section: 'clusters'});
  store.dispatch({type: 'section', section: 'logins'});
  await settle();
  expect(read).toEqual(['ssh:host-b']);
  expect(store.getState().views['ssh:host-b']?.compute).toEqual({
    kind: 'ready',
    offered: {kind: 'offered', value: COMPUTE},
  });
  expect(store.getState().views['ssh:host-b']?.logins).toEqual({
    kind: 'ready',
    offered: {kind: 'unsupported'},
  });
});

test('a call that throws says something unexpected, and one the main process refused keeps its words', async () => {
  const store = createMachineSettingsStore(
    bridge({
      compute: () => {
        throw new TypeError('bridge.compute is not a function');
      },
      agentLogins: () =>
        Promise.resolve({ok: false, error: 'host-b is unreachable.', authNeeded: false}),
    }),
  );
  store.dispatch({type: 'focus', host: 'ssh:host-b', section: 'clusters'});
  await settle();
  const view = store.getState().views['ssh:host-b'];
  expect(view?.compute).toEqual({kind: 'failed', error: UNEXPECTED_FAILURE});
  expect(view?.logins).toEqual({kind: 'failed', error: 'host-b is unreachable.'});
});

test('a connection failure is kept with its typed cause beside the raw words', async () => {
  const store = createMachineSettingsStore(
    bridge({
      compute: () =>
        Promise.resolve({
          ok: false,
          error: 'the host helper for /srv/x exited with 1',
          authNeeded: false,
          cause: 'incompatible',
        }),
    }),
  );
  store.dispatch({type: 'focus', host: 'ssh:host-b', section: 'clusters'});
  await settle();
  expect(store.getState().views['ssh:host-b']?.compute).toEqual({
    kind: 'failed',
    error: 'the host helper for /srv/x exited with 1',
    cause: 'incompatible',
  });
});

test('a cluster is tested and saved through the bridge, with the cluster the form describes', async () => {
  const sent: string[] = [];
  const store = createMachineSettingsStore(
    bridge({
      testCluster: (host, cluster) => {
        sent.push(`test ${host} ${cluster.name}`);
        return Promise.resolve({
          ok: true,
          value: {outcome: 'passed', hardware: {}, partitions: []},
        });
      },
      addCluster: (host, cluster) => {
        sent.push(`add ${host} ${cluster.name}`);
        return Promise.resolve({ok: true, value: COMPUTE});
      },
    }),
  );
  const host = 'ssh:host-b';
  store.dispatch({type: 'focus', host, section: 'clusters'});
  store.dispatch({type: 'open-form', host});
  for (const [key, value] of [
    ['name', 'lab'],
    ['host', 'lab-login'],
    ['workspace', '/w'],
  ] as const) {
    store.dispatch({type: 'field', host, key, value});
  }
  store.dispatch({type: 'test', host});
  await settle();
  store.dispatch({type: 'save', host});
  await settle();
  expect(sent).toEqual([`test ${host} lab`, `add ${host} lab`]);
  expect(store.getState().views[host]?.form).toBeNull();
});
