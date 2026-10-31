import {describe, expect, test} from 'bun:test';
import {
  ConnectionManager,
  hostRuns,
  registryClusters,
  registryReads,
  registrySetup,
  SubscriptionFailed,
} from '@vibesys/connection';
import {FakeLink, hubRig, olderHost, settle} from '@vibesys/connection/testing';
import {type HostId, hostKey} from '@vibesys/hosts';
import type {MachineReport} from '@vibesys/ipc';
import {SettingsStore, withHostSettings, withoutHost} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {runs} from '@vibesys/subscription';
import {FakeClock} from '@vibesys/transport/testing';
import {MachinesMain, type MachinesMainDeps} from './index.js';

const LOCAL: HostId = {kind: 'local'};
const HOST_B: HostId = {kind: 'ssh', alias: 'host-b'};
const HOST_A: HostId = {kind: 'ssh', alias: 'host-a'};
const CONFIG = {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000};

/** The machines over real settings and a real connection manager, each host behind its own Fake. */
async function subject(
  links: ReadonlyMap<string, FakeLink>,
  extra?: Partial<MachinesMainDeps>,
): Promise<{
  readonly machines: MachinesMain;
  readonly settings: SettingsStore;
  readonly manager: ConnectionManager;
}> {
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  await settings.updateHosts(current =>
    withHostSettings(withHostSettings(current, HOST_B, {checkout: '~/vibesys'}), HOST_A, {
      checkout: '/mnt/data/vibesys',
    }),
  );
  const manager = new ConnectionManager({
    linkFor: id => {
      const link = links.get(hostKey(id));
      if (link === undefined) throw new Error(`no fake link for ${hostKey(id)}`);
      return link;
    },
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    config: CONFIG,
    log: () => {},
  });
  const checkout = async (id: HostId): Promise<string | null> => {
    const saved = (await settings.hosts()).settings.hosts[hostKey(id)]?.checkout ?? null;
    return saved ?? (id.kind === 'local' ? '/Users/me/vibesys' : null);
  };
  const machines = new MachinesMain({
    settings,
    removeHost: id => settings.updateHosts(current => withoutHost(current, id)).then(() => {}),
    checkout,
    ...extra,
  });
  return {machines, settings, manager};
}

describe('MachinesMain', () => {
  test('lists This Mac first, then every saved host by name, each with its checkout', async () => {
    const {machines, manager} = await subject(new Map());
    await using _ = manager;
    expect(await machines.machines()).toEqual([
      {host: 'local', label: 'This Mac', checkout: '/Users/me/vibesys'},
      {host: 'ssh:host-a', label: 'host-a', checkout: '/mnt/data/vibesys'},
      {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'},
    ]);
  });

  test('transport v2 inspects through the registry and a moved revision is stale', async () => {
    let revision = 3n;
    let release: () => void = () => {};
    const gate = new Promise<void>(resolve => {
      release = resolve;
    });
    let started: () => void = () => {};
    const startedPromise = new Promise<void>(resolve => {
      started = resolve;
    });
    const {machines, manager} = await subject(new Map(), {
      selectionRevision: () => revision,
      setup: {
        completePaths: async () => {
          throw new Error('unused');
        },
        inspectCandidate: async () => {
          started();
          await gate;
          return {state: 'too_old', version: '0.0.9', min: '0.1.1'};
        },
      },
    });
    await using _ = manager;
    const pending = machines.inspect(HOST_B, '/home/me/vibesys-old', 3n);
    await startedPromise;
    revision = 4n;
    release();
    await expect(pending).rejects.toThrow('stale selection');
    revision = 4n;
    await expect(machines.inspect(HOST_B, '/home/me/vibesys-old', 3n)).rejects.toThrow(
      'stale selection',
    );
  });

  test('removing a machine forgets only it; This Mac stays on the app’s own checkout', async () => {
    const {machines, settings, manager} = await subject(new Map());
    await using _ = manager;
    expect((await machines.remove(HOST_B)).map(entry => entry.host)).toEqual([
      'local',
      'ssh:host-a',
    ]);
    expect(Object.keys((await settings.hosts()).settings.hosts)).toEqual(['ssh:host-a']);
    expect((await machines.remove(LOCAL)).map(entry => entry.host)).toEqual([
      'local',
      'ssh:host-a',
    ]);
  });

  describe('agent logins', () => {
    test('transport v2 starts the login on the actor', async () => {
      const link = new FakeLink();
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]), {
        logins: {
          start: async () => ({outcome: 'started', instruction: 'Open the link'}),
          cancelHost: () => {},
        },
      });
      await using _ = manager;
      expect(await machines.startLogin(HOST_B, 'claude')).toEqual({
        outcome: 'started',
        instruction: 'Open the link',
      });
    });
  });

  describe('run transport v2', () => {
    const facts = {
      checkout: {root: '/src/vibesys', version: '0.1.1', commit: 'abc123', dirty: false},
      hardware: {cpu_cores: 8, gpus: [], accelerator: 'cpu' as const},
      docker: {reachable: true as const, version: '27.3.1'},
      harness_clis: ['codex'],
      runs: {live: 0, ended_today: 0},
    };
    const settings = {
      config_path: '/settings',
      clusters: [
        {
          cluster: {
            name: 'campus',
            reach: {kind: 'ssh' as const, host: 'host-a'},
            workspace: '/work',
            sbatch_options: [],
            hardware: null,
          },
          source: 'slurm_toml' as const,
        },
      ],
      modal: {token_present: false},
    };

    test('an on switch reads the registry and does not open the helper', async () => {
      let expected: string | null = 'unset';
      const {machines, manager} = await subject(new Map(), {
        registry: {
          describeMachine: async (_host, checkout) => {
            expected = checkout;
            return facts;
          },
          listCompute: async () => settings,
        },
        runs: {alive: async () => 0},
      });
      await using _ = manager;
      expect(await machines.report(HOST_B, '/mnt/data/vibesys')).toEqual({
        kind: 'ready',
        facts,
      });
      expect(expected).toBe('/mnt/data/vibesys');
      expect(await machines.compute(HOST_B)).toEqual({kind: 'offered', value: settings});
    });

    test('an on switch counts live runs from the runs subscription', async () => {
      const {machines, manager} = await subject(new Map(), {
        registry: {
          describeMachine: async () => facts,
          listCompute: async () => settings,
        },
        runs: {alive: async () => 2},
      });
      await using _ = manager;
      const report = await machines.report(HOST_B);
      expect(report.kind === 'ready' && report.facts?.runs.live).toBe(2);
    });

    test('a host that did not say how many runs it holds is unavailable, never a count of none', async () => {
      const {machines, manager} = await subject(new Map(), {
        registry: {
          describeMachine: async () => facts,
          listCompute: async () => settings,
        },
        runs: {
          alive: async () => {
            throw new SubscriptionFailed('unsupported', false);
          },
        },
      });
      await using _ = manager;
      expect((await machines.report(HOST_B)).kind).toBe('unavailable');
    });

    test('a host whose runs stream never answers is unavailable at the topic’s first-frame bound, not after its attempts run out', async () => {
      const rig = hubRig();
      const {machines, manager} = await subject(new Map(), {
        registry: {
          describeMachine: async () => facts,
          listCompute: async () => settings,
        },
        runs: hostRuns(rig.subscriptions),
      });
      await using _ = manager;
      let report: MachineReport | undefined;
      void machines.report(HOST_B).then(reported => (report = reported));
      await settle();
      // The stream opens and the host says nothing, as every attempt after it would.
      rig.gateway.streams[0]?.reached('handshake');
      rig.clock.advanceBy(runs.policy.firstFrameMs - 1);
      await settle();
      expect(report).toBeUndefined();
      rig.clock.advanceBy(1);
      await settle();
      expect(report?.kind).toBe('unavailable');
    });

    test('an on switch changes clusters through the registry and does not open the helper', async () => {
      const calls: string[] = [];
      const saved = {
        config_path: '/settings',
        clusters: [],
        modal: {token_present: false},
      };
      const passed = {
        outcome: 'passed' as const,
        hardware: {gpus_per_node: 1},
        partitions: ['gpu'],
      };
      const {machines, manager} = await subject(new Map(), {
        clusters: {
          put: async () => {
            calls.push('put');
            return saved;
          },
          remove: async (_host, name) => {
            calls.push(`remove:${name}`);
            return saved;
          },
          test: async () => {
            calls.push('test');
            return passed;
          },
        },
      });
      await using _ = manager;
      const cluster = {
        name: 'lab',
        reach: {kind: 'ssh' as const, host: 'host-a'},
        workspace: '/work',
      };
      expect(await machines.testCluster(HOST_B, cluster)).toEqual(passed);
      expect(await machines.addCluster(HOST_B, cluster)).toEqual(saved);
      expect(await machines.removeCluster(HOST_B, 'lab')).toEqual(saved);
      expect(calls).toEqual(['test', 'put', 'remove:lab']);
    });

    test('a host whose VibeSys predates the registry needs an update on every path, with no host text', async () => {
      const checkout = async () => '/src/checkout';
      const {machines, manager} = await subject(new Map(), {
        registry: registryReads({checkout, unary: olderHost.read}),
        setup: registrySetup({unary: olderHost.read}),
        clusters: registryClusters({checkout, unary: olderHost.mutate}),
        runs: {alive: async () => 0},
      });
      await using _ = manager;
      const cluster = {
        name: 'lab',
        reach: {kind: 'ssh' as const, host: 'host-a'},
        workspace: '/work',
      };
      expect(await machines.inspect(HOST_B, '/src/checkout')).toEqual({state: 'unsupported'});
      expect(await machines.report(HOST_B)).toEqual({kind: 'needs-update', facts: null});
      expect(await machines.compute(HOST_B)).toEqual({kind: 'unsupported'});
      for (const change of [
        () => machines.testCluster(HOST_B, cluster),
        () => machines.addCluster(HOST_B, cluster),
        () => machines.removeCluster(HOST_B, 'lab'),
      ]) {
        const error = await change().then(
          () => null,
          (failure: unknown) => failure as Error,
        );
        expect(error?.message).toContain("This host's VibeSys needs an update");
        expect(error?.message).not.toMatch(/usage|invalid choice|does not provide/i);
      }
    });

    test('Reconnect on a machine that needs an update asks the same question again', async () => {
      let asked = 0;
      const checkout = async () => '/src/checkout';
      const counting = async (...args: Parameters<typeof olderHost.read>) => {
        asked += 1;
        return olderHost.read(...args);
      };
      const {machines, manager} = await subject(new Map(), {
        registry: registryReads({checkout, unary: counting}),
        runs: {alive: async () => 0},
      });
      await using _ = manager;
      expect(await machines.report(HOST_B)).toEqual({kind: 'needs-update', facts: null});
      expect(await machines.report(HOST_B)).toEqual({kind: 'needs-update', facts: null});
      expect(asked).toBe(2);
    });

    test('an on switch without the registry throws instead of using the helper', async () => {
      const {machines, manager} = await subject(new Map(), {});
      await using _ = manager;
      await expect(machines.report(HOST_B)).rejects.toThrow('registry reads are missing');
      await expect(machines.compute(HOST_B)).rejects.toThrow('registry reads are missing');
      await expect(
        machines.addCluster(HOST_B, {
          name: 'lab',
          reach: {kind: 'ssh', host: 'host-a'},
          workspace: '/work',
        }),
      ).rejects.toThrow('cluster registry is missing');
    });
  });
});
