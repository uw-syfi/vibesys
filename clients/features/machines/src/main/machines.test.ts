import {describe, expect, test} from 'bun:test';
import {
  ConnectionManager,
  HOST_UNSUPPORTED_MESSAGE,
  registryClusters,
  registryReads,
  registrySetup,
} from '@vibesys/connection';
import {FakeLink, olderHost, USAGE_DUMP} from '@vibesys/connection/testing';
import {type HostId, hostKey, REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import {SettingsStore, withHostSettings, withoutHost} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
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
    connection: async id => {
      const path = await checkout(id);
      if (path === null) throw new Error('no checkout');
      return manager.acquire(id, path);
    },
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

  test('a machine whose helper lacks a feature the app needs is "needs-update", not ready', async () => {
    for (const lacking of REQUIRED_HELPER_FEATURES) {
      const link = new FakeLink();
      link.helper.features.delete(lacking);
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
      await using _ = manager;
      const report = await machines.report(HOST_B);
      expect(report.kind).toBe('needs-update');
      expect(report.kind === 'needs-update' && report.facts !== null).toBe(true);
    }
    const {machines, manager} = await subject(new Map([['ssh:host-b', new FakeLink()]]));
    await using _ = manager;
    expect((await machines.report(HOST_B)).kind).toBe('ready');
  });

  test('a machine’s status follows from its connection and its helper’s description', async () => {
    const ready = new FakeLink();
    const missing = new FakeLink();
    missing.helper.bootstrapExit = {code: 65, stdout: 'nodir'};
    const tooOld = new FakeLink();
    tooOld.helper.bootstrapExit = {code: 64, stdout: 'predates 0.0.9'};
    const locked = new FakeLink();
    locked.linkFault = {cause: 'auth', detail: 'the SSH key needs its passphrase'};
    const cases: readonly [FakeLink, string][] = [
      [ready, 'ready'],
      [missing, 'checkout-not-found'],
      [tooOld, 'unavailable'],
      [locked, 'sign-in'],
    ];
    for (const [link, kind] of cases) {
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
      await using _ = manager;
      const report = await machines.report(HOST_B);
      expect(report.kind).toBe(kind as typeof report.kind);
      if (report.kind === 'ready') expect(report.facts).toEqual(link.helper.host.machine);
    }
  });

  test('a path’s inspection is the helper’s own', async () => {
    const link = new FakeLink();
    link.helper.host.checkouts.set('/home/me/vibesys-old', {
      state: 'too_old',
      version: '0.0.9',
      min: '0.1.0',
    });
    const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
    await using _ = manager;
    expect(await machines.inspect(HOST_B, '/home/me/vibesys-old')).toEqual({
      state: 'too_old',
      version: '0.0.9',
      min: '0.1.0',
    });
    expect(await machines.inspect(HOST_B, '/nowhere')).toEqual({state: 'not_found'});
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
      transportV2: async () => true,
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

  describe('clusters and clouds', () => {
    const cluster = {
      name: 'lab-h100',
      reach: {kind: 'ssh' as const, host: 'lab-login'},
      workspace: '/scratch/vibesys',
      sbatch_options: [],
    };
    const passed = {outcome: 'passed' as const, hardware: {gpus_per_node: 8}, partitions: ['gpu']};

    test('a cluster is tested, then saved, then listed, then removed, on the machine it is for', async () => {
      const link = new FakeLink();
      link.helper.host.clusterTests.set('lab-login', passed);
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
      await using _ = manager;
      expect(await machines.testCluster(HOST_B, cluster)).toEqual(passed);
      expect(link.helper.host.compute.clusters).toEqual([]);
      const saved = await machines.addCluster(HOST_B, cluster);
      expect(saved.clusters.map(listed => listed.cluster.name)).toEqual(['lab-h100']);
      expect(await machines.compute(HOST_B)).toEqual({kind: 'offered', value: saved});
      const left = await machines.removeCluster(HOST_B, 'lab-h100');
      expect(left.clusters).toEqual([]);
    });

    test('a cluster that fails its test is not saved, and the helper’s account is the error', async () => {
      const link = new FakeLink();
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
      await using _ = manager;
      expect(await machines.testCluster(HOST_B, cluster)).toMatchObject({
        outcome: 'failed',
        stage: 'reach',
      });
      await expect(machines.addCluster(HOST_B, cluster)).rejects.toThrow(
        'Could not save the cluster on host-b: reach: cannot reach lab-login',
      );
      expect(link.helper.host.compute.clusters).toEqual([]);
    });

    test('a helper that predates machine compute offers nothing, and cannot change it', async () => {
      const link = new FakeLink();
      link.helper.features.delete('machine_compute');
      link.helper.features.delete('agent_logins');
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
      await using _ = manager;
      expect(await machines.compute(HOST_B)).toEqual({kind: 'unsupported'});
      expect(await machines.agentLogins(HOST_B)).toEqual({kind: 'unsupported'});
      await expect(machines.removeCluster(HOST_B, 'lab-h100')).rejects.toThrow(
        'Could not remove the cluster on host-b',
      );
    });
  });

  describe('agent logins', () => {
    test('each harness’s login is the helper’s, and starting one is its own answer', async () => {
      const link = new FakeLink();
      link.helper.host.logins = [
        {
          provider: 'claude',
          provider_name: 'Claude Code',
          installed: true,
          state: 'signed_out',
          api_key_variables: ['ANTHROPIC_API_KEY'],
          detail: 'Not logged in',
          can_start_login: true,
        },
      ];
      link.helper.host.loginStarts.set('claude', {
        outcome: 'started',
        instruction: 'Open the link',
      });
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]));
      await using _ = manager;
      expect(await machines.agentLogins(HOST_B)).toEqual({
        kind: 'offered',
        value: link.helper.host.logins,
      });
      expect(await machines.startLogin(HOST_B, 'claude')).toEqual({
        outcome: 'started',
        instruction: 'Open the link',
      });
      expect(await machines.startLogin(HOST_B, 'codex')).toEqual({
        outcome: 'unavailable',
        reason: 'no_login_command',
      });
    });

    test('transport v2 starts the login on the actor', async () => {
      const link = new FakeLink();
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]), {
        transportV2: async () => true,
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

    test('an off switch still asks the helper', async () => {
      const link = new FakeLink();
      const {machines, manager} = await subject(new Map([['ssh:host-b', link]]), {
        transportV2: async () => false,
      });
      await using _ = manager;
      expect((await machines.report(HOST_B)).kind).toBe('ready');
    });

    test('an on switch reads the registry and does not open the helper', async () => {
      let expected: string | null = 'unset';
      const {machines, manager} = await subject(new Map(), {
        transportV2: async () => true,
        registry: {
          describeMachine: async (_host, checkout) => {
            expected = checkout;
            return facts;
          },
          listCompute: async () => settings,
        },
        runs: {live: async () => 0},
      });
      await using _ = manager;
      expect(await machines.report(HOST_B, '/mnt/data/vibesys')).toEqual({
        kind: 'ready',
        facts,
      });
      expect(expected).toBe('/mnt/data/vibesys');
      expect(await machines.compute(HOST_B)).toEqual({kind: 'offered', value: settings});
    });

    test('an on switch counts live runs from WatchRuns', async () => {
      const {machines, manager} = await subject(new Map(), {
        transportV2: async () => true,
        registry: {
          describeMachine: async () => facts,
          listCompute: async () => settings,
        },
        runs: {live: async () => 2},
      });
      await using _ = manager;
      const report = await machines.report(HOST_B);
      expect(report.kind === 'ready' && report.facts?.runs.live).toBe(2);
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
        transportV2: async () => true,
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

    test('a host whose VibeSys predates the registry is "needs-update" on every machine read, with no usage dump', async () => {
      const checkout = async () => '/src/checkout';
      const {machines, manager} = await subject(new Map(), {
        transportV2: async () => true,
        registry: registryReads({checkout, unary: olderHost.read}),
        setup: registrySetup({unary: olderHost.read}),
        clusters: registryClusters({checkout, unary: olderHost.mutate}),
        runs: {live: async () => 0},
      });
      await using _ = manager;
      expect(await machines.report(HOST_B)).toEqual({kind: 'needs-update', facts: null});
      expect(await machines.compute(HOST_B)).toEqual({kind: 'unsupported'});
      expect(await machines.inspect(HOST_B, '/src/checkout')).toEqual({state: 'unsupported'});
      const cluster = {
        name: 'lab',
        reach: {kind: 'ssh' as const, host: 'host-a'},
        workspace: '/work',
      };
      for (const change of [
        () => machines.testCluster(HOST_B, cluster),
        () => machines.addCluster(HOST_B, cluster),
        () => machines.removeCluster(HOST_B, 'lab'),
      ]) {
        const error = await change().catch((caught: unknown) => caught);
        expect((error as Error).message).toContain(HOST_UNSUPPORTED_MESSAGE);
        expect((error as Error).message).not.toContain(USAGE_DUMP);
      }
    });

    test('an on switch without the registry throws instead of using the helper', async () => {
      const {machines, manager} = await subject(new Map(), {transportV2: async () => true});
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
