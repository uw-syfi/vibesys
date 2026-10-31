import {describe, expect, test} from 'bun:test';
import {type MachineDescription, type MachineProblem, problemWord} from '@vibesys/hosts';
import type {
  ClusterTest,
  ComputeSettings,
  MachineEntry,
  MachineReport,
  Offered,
  SlurmClusterSettings,
} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {RETRYABLE_REASONS, TERMINAL_REASONS} from '@vibesys/subscription';
import fc from 'fast-check';
import {
  cardAction,
  cardStatus,
  clockWord,
  hardwareLine,
  initialMachines,
  type Machine,
  type MachinesAction,
  type MachinesRequest,
  type MachinesState,
  machineHardware,
  machineLoad,
  machineStatus,
  machinesAttention,
  machinesStep,
  placeRows,
  type StatsView,
  statusWord,
} from './index.js';

const POOL: readonly SlurmClusterSettings[] = ['alpha', 'beta', 'gamma', 'delta', 'epsilon'].map(
  name => ({
    name,
    reach: name === 'beta' ? {kind: 'login_node'} : {kind: 'ssh', host: `${name}-login`},
    workspace: '/scratch/vibesys',
    hardware:
      name === 'gamma' ? null : {accelerator: 'cuda', gpu_models: ['h100'], gpus_per_node: 8},
  }),
);

const FACTS = (gpus: number, live: number): MachineDescription => ({
  checkout: null,
  hardware: {
    cpu_cores: 32,
    gpus: Array.from({length: gpus}, () => ({vendor: 'nvidia', model: 'h100'})),
    accelerator: gpus > 0 ? 'cuda' : 'cpu',
  },
  docker: {reachable: true, version: '29.1'},
  harness_clis: [],
  runs: {live, ended_today: 0},
});

const PASSED: ClusterTest = {
  outcome: 'passed',
  hardware: {accelerator: 'rocm', gpu_models: ['mi300x'], gpus_per_node: 8},
  partitions: [],
};
const FAILED: ClusterTest = {outcome: 'failed', stage: 'reach', detail: 'no route to host'};

const offered = (names: readonly string[], modal = false): Offered<ComputeSettings> => ({
  kind: 'offered',
  value: {
    config_path: '/home/me/.config/vibesys/compute.toml',
    clusters: POOL.filter(cluster => names.includes(cluster.name)).map(cluster => ({
      cluster,
      source: 'vibesys' as const,
    })),
    modal: {token_present: modal},
  },
});

/** A store with `hosts` listed and each reported ready, and the requests each answer made. */
function ready(hosts: readonly string[]): MachinesState {
  const entries: MachineEntry[] = hosts.map(host => ({host, label: host, checkout: '~/vibesys'}));
  let {state} = initialMachines();
  state = machinesStep(state, {type: 'listed', token: 1, result: {ok: true, value: entries}}).state;
  for (const machine of state.machines) {
    state = machinesStep(state, {
      type: 'reported',
      token: machine.checking ?? 0,
      host: machine.host,
      result: {ok: true, value: {kind: 'ready', facts: FACTS(2, 1)}},
    }).state;
  }
  return state;
}

function loadingToken(state: MachinesState, host: string): number {
  const reading = state.places[host]?.reading;
  return reading?.kind === 'loading' ? reading.token : 0;
}

function withCompute(state: MachinesState, host: string, value: Offered<ComputeSettings>) {
  return machinesStep(state, {
    type: 'computed',
    token: loadingToken(state, host),
    host,
    result: {ok: true, value},
  }).state;
}

/** Test `name` on `host` and answer with `result` at `at`. */
function tested(
  state: MachinesState,
  host: string,
  name: string,
  result: ClusterTest,
  at: number,
): MachinesState {
  const asked = machinesStep(state, {type: 'test-cluster', host, name});
  const request = asked.requests[0];
  expect(request?.kind).toBe('test-cluster');
  return machinesStep(asked.state, {
    type: 'tested',
    token: request?.token ?? 0,
    host,
    name,
    at,
    result: {ok: true, value: result},
  }).state;
}

const checkedWord = (state: MachinesState): string | undefined =>
  placeRows(state, state.machines[0] as never)[0]?.checked.word;

function expectedWord(at: number, since: number | null): string {
  return since === null ? `Checked ${clockWord(at)}` : `Unreachable since ${clockWord(since)}`;
}

const rowNames = (state: MachinesState, host: string): string[] =>
  placeRows(state, state.machines.find(machine => machine.host === host) as never).map(
    row => row.name,
  );

describe('the places under each machine', () => {
  test('property: a machine lists exactly its own clusters, and Modal when its token is found', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const random = new Seeded(seed);
      const hosts = ['local', 'ssh:host-a', 'ssh:host-b'];
      const own = new Map(
        hosts.map(host => [host, POOL.map(c => c.name).filter(() => random.bool())] as const),
      );
      const modal = new Map(hosts.map(host => [host, random.bool()] as const));
      let state = ready(hosts);
      for (const host of hosts) {
        state = withCompute(state, host, offered(own.get(host) ?? [], modal.get(host)));
      }
      for (const host of hosts) {
        const expected = [...(own.get(host) ?? []), ...(modal.get(host) ? ['Modal'] : [])];
        expect(rowNames(state, host).sort()).toEqual(expected.sort());
      }
    }
  });

  test('a machine that is not online lists nothing, and a reply for a stale reading is dropped', () => {
    let state = ready(['ssh:host-a']);
    const stale = loadingToken(state, 'ssh:host-a');
    state = machinesStep(state, {type: 'reconnect', host: 'ssh:host-a'}).state;
    const check = state.machines[0]?.checking ?? 0;
    state = machinesStep(state, {
      type: 'reported',
      token: check,
      host: 'ssh:host-a',
      result: {ok: true, value: {kind: 'ready', facts: FACTS(0, 0)}},
    }).state;
    // The answer to the first reading arrives after the second was asked: it names nothing.
    const late = machinesStep(state, {
      type: 'computed',
      token: stale,
      host: 'ssh:host-a',
      result: {ok: true, value: offered(['alpha'])},
    });
    expect(rowNames(late.state, 'ssh:host-a')).toEqual([]);
    state = withCompute(late.state, 'ssh:host-a', offered(['alpha']));
    expect(rowNames(state, 'ssh:host-a')).toEqual(['alpha']);

    const down = machinesStep(state, {type: 'reconnect', host: 'ssh:host-a'});
    const gone = machinesStep(down.state, {
      type: 'reported',
      token: down.state.machines[0]?.checking ?? 0,
      host: 'ssh:host-a',
      result: {ok: true, value: {kind: 'unavailable', detail: 'timed out'}},
    }).state;
    expect(cardStatus(gone.machines[0] as never)).toEqual({word: 'Unreachable', tone: 'err'});
    expect(rowNames(gone, 'ssh:host-a')).toEqual([]);
  });

  test('Test asks for the cluster as the machine lists it, once at a time, and says when it checked', () => {
    let state = withCompute(ready(['ssh:host-a']), 'ssh:host-a', offered(['alpha', 'beta']));
    const [first] = placeRows(state, state.machines[0] as never);
    expect(first?.checked.word).toBe('Not checked');
    const asked = machinesStep(state, {type: 'test-cluster', host: 'ssh:host-a', name: 'alpha'});
    expect(asked.requests).toEqual([
      expect.objectContaining({kind: 'test-cluster', cluster: POOL[0]}),
    ]);
    // A second press while testing asks nothing; a cluster it does not list asks nothing.
    expect(
      machinesStep(asked.state, {type: 'test-cluster', host: 'ssh:host-a', name: 'alpha'}).requests,
    ).toEqual([]);
    expect(
      machinesStep(state, {type: 'test-cluster', host: 'ssh:host-a', name: 'nope'}).requests,
    ).toEqual([]);
    expect(placeRows(asked.state, asked.state.machines[0] as never)[0]?.testing).toBe(true);

    state = tested(state, 'ssh:host-a', 'alpha', PASSED, 5_000);
    const row = placeRows(state, state.machines[0] as never)[0];
    expect(row?.testing).toBe(false);
    expect(row?.checked).toEqual({word: `Checked ${clockWord(5_000)}`, tone: 'ok'});
    expect(clockWord(5_000)).toMatch(/^\d\d:\d\d$/);
  });

  test('property: unreachable since is when the failures began, and a pass ends them', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const random = new Seeded(seed);
      let state = withCompute(ready(['local']), 'local', offered(['alpha']));
      let since: number | null = null;
      for (let step = 1; step <= 8; step += 1) {
        const at = step * 1000;
        const fails = random.bool();
        state = tested(state, 'local', 'alpha', fails ? FAILED : PASSED, at);
        since = fails ? (since ?? at) : null;
        expect(checkedWord(state)).toBe(expectedWord(at, since));
        expect(machinesAttention(state) === 'unavailable').toBe(since !== null);
      }
    }
  });

  test('a cluster measured by a test shows what the test read when it was never stated', () => {
    let state = withCompute(ready(['local']), 'local', offered(['gamma']));
    expect(placeRows(state, state.machines[0] as never)[0]?.hardware).toBe('–');
    state = tested(state, 'local', 'gamma', PASSED, 1);
    expect(placeRows(state, state.machines[0] as never)[0]?.hardware).toBe('8× MI300X');
  });

  test('a removed cluster takes its test with it, and the reply of a test of it is dropped', () => {
    let state = withCompute(ready(['local']), 'local', offered(['alpha']));
    const asked = machinesStep(state, {type: 'test-cluster', host: 'local', name: 'alpha'});
    const token = asked.requests[0]?.token ?? 0;
    state = asked.state;
    state = machinesStep(state, {type: 'reconnect', host: 'local'}).state;
    state = machinesStep(state, {
      type: 'reported',
      token: state.machines[0]?.checking ?? 0,
      host: 'local',
      result: {ok: true, value: {kind: 'ready', facts: FACTS(0, 0)}},
    }).state;
    state = withCompute(state, 'local', offered([]));
    state = machinesStep(state, {
      type: 'tested',
      token,
      host: 'local',
      name: 'alpha',
      at: 9,
      result: {ok: true, value: FAILED},
    }).state;
    expect(rowNames(state, 'local')).toEqual([]);
    expect(machinesAttention(state)).toBeNull();
  });
});

describe('reconnect and load', () => {
  test('the card offers Sign in, Reconnect or nothing, by the machine status', () => {
    const state = ready(['ssh:host-a']);
    const machine = state.machines[0] as never as Machine;
    expect(cardAction(machine)).toBeNull();
    const withReport = (report: Machine['report']): Machine => ({...machine, report});
    expect(cardAction(withReport({kind: 'unavailable', detail: 'timed out'}))).toBe('reconnect');
    expect(cardAction(withReport({kind: 'sign-in', detail: 'passphrase'}))).toBe('sign-in');
    expect(cardAction(withReport({kind: 'checkout-not-found', detail: 'gone'}))).toBeNull();
    expect(cardAction({...machine, checking: 1})).toBeNull();
  });

  test('a machine and a cluster write the same GPU the same way, and an unknown machine has no line', () => {
    const state = ready(['local']);
    const machine = state.machines[0] as never as Machine;
    expect(machineHardware(machine)).toBe('32 CPU · 2× H100');
    expect(machineHardware({...machine, report: null})).toBeNull();
    expect(hardwareLine({accelerator: 'cuda', gpu_models: ['nvidia_h100'], gpus_per_node: 2})).toBe(
      '2× H100',
    );
  });

  test('Reconnect signs a machine in when it waits for that, and checks it again otherwise', () => {
    let state = ready(['ssh:host-a']);
    const again = machinesStep(state, {type: 'reconnect', host: 'ssh:host-a'});
    expect(again.requests).toEqual([expect.objectContaining({kind: 'report', host: 'ssh:host-a'})]);
    expect(machineStatus(again.state.machines[0] as never)).toBe('checking');
    // Pressed again while checking: nothing more is asked.
    expect(machinesStep(again.state, {type: 'reconnect', host: 'ssh:host-a'}).requests).toEqual([]);

    state = machinesStep(again.state, {
      type: 'reported',
      token: again.state.machines[0]?.checking ?? 0,
      host: 'ssh:host-a',
      result: {ok: true, value: {kind: 'sign-in', detail: 'the key needs its passphrase'}},
    }).state;
    const signIn: readonly MachinesRequest[] = machinesStep(state, {
      type: 'reconnect',
      host: 'ssh:host-a',
    }).requests;
    expect(signIn).toEqual([expect.objectContaining({kind: 'sign-in', host: 'ssh:host-a'})]);
  });

  test('property: how busy a machine is stays between none and all of its slots', () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const random = new Seeded(seed);
      const gpus = random.int(9);
      const live = random.int(20);
      const state = ready(['local']);
      const machine = {
        ...state.machines[0],
        report: {kind: 'ready', facts: FACTS(gpus, live)},
      } as never;
      const load = machineLoad(machine, live);
      expect(load?.slots).toBe(Math.max(1, gpus));
      // Its load is the run list's live count, not the facts' count of the moment it was checked.
      expect(machineLoad(machine, null)).toBeNull();
      expect(load?.share).toBeGreaterThanOrEqual(0);
      expect(load?.share).toBeLessThanOrEqual(1);
      expect(load?.share).toBe(Math.min(1, live / Math.max(1, gpus)));
    }
  });

  test('the rail names a machine by its own status: an old one needs an update, not unreachable', () => {
    const cases: readonly (readonly [MachineReport, MachineProblem | null])[] = [
      [{kind: 'ready', facts: FACTS(0, 0)}, null],
      [{kind: 'needs-update', facts: FACTS(0, 0)}, 'needs-update'],
      [{kind: 'unavailable', detail: 'timed out'}, 'unavailable'],
      [{kind: 'sign-in', detail: 'waiting'}, 'sign-in'],
      [{kind: 'checkout-not-found', detail: 'gone'}, 'checkout-not-found'],
    ];
    for (const [report, problem] of cases) {
      const state = ready(['ssh:host-a']);
      const machines = state.machines.map(each => ({...each, report, checking: null}));
      expect(machinesAttention({...state, machines})).toBe(problem);
    }
    const checking = ready(['ssh:host-a']);
    const machines = checking.machines.map(each => ({...each, report: null, checking: 7}));
    expect(machinesAttention({...checking, machines})).toBeNull();
  });

  test('the card and the rail word each status alike', () => {
    const reports: readonly MachineReport[] = [
      {kind: 'needs-update', facts: FACTS(0, 0)},
      {kind: 'unavailable', detail: 'timed out'},
      {kind: 'sign-in', detail: 'waiting'},
      {kind: 'checkout-not-found', detail: 'gone'},
    ];
    for (const report of reports) {
      const state = ready(['ssh:host-a']);
      const machine = {...(state.machines[0] as Machine), report, checking: null};
      const problem = machinesAttention({...state, machines: [machine]});
      expect(problem).not.toBeNull();
      expect(cardStatus(machine).word).toBe(problemWord(problem as MachineProblem));
      expect(statusWord(machineStatus(machine))).toBe(cardStatus(machine).word);
    }
  });

  test('a machine that cannot be reached needs attention', () => {
    const state = ready(['ssh:host-a']);
    expect(machinesAttention(state)).toBeNull();
    const action: MachinesAction = {type: 'reconnect', host: 'ssh:host-a'};
    const checking = machinesStep(state, action).state;
    const down = machinesStep(checking, {
      type: 'reported',
      token: checking.machines[0]?.checking ?? 0,
      host: 'ssh:host-a',
      result: {ok: true, value: {kind: 'unavailable', detail: 'timed out'}},
    }).state;
    expect(machinesAttention(down)).toBe('unavailable');
  });
});

describe('one status for a card, whatever its check and its stream each say', () => {
  const REPORTS: readonly (MachineReport | 'checking')[] = [
    {kind: 'ready', facts: FACTS(2, 1)},
    'checking',
    {kind: 'sign-in', detail: 'waiting'},
    {kind: 'unavailable', detail: 'deadline exceeded after 10 s'},
    {kind: 'checkout-not-found', detail: 'gone'},
    {kind: 'needs-update', facts: FACTS(0, 0)},
  ];
  const machineWith = (report: MachineReport | 'checking'): Machine => {
    const machine = ready(['ssh:host-a']).machines[0] as Machine;
    return report === 'checking'
      ? {...machine, report: null, checking: 3}
      : {...machine, report, checking: null};
  };
  const VIEWS: fc.Arbitrary<StatsView> = fc.oneof(
    fc.constant({kind: 'waiting'} as const),
    fc.constant({kind: 'values', gauges: [], stale: null} as const),
    fc.boolean().map(
      reconnecting =>
        ({
          kind: 'values',
          gauges: [],
          stale: {valueAt: 1, reconnecting},
        }) as const satisfies StatsView,
    ),
    fc
      .constantFrom(...RETRYABLE_REASONS, ...TERMINAL_REASONS)
      .map(reason => ({kind: 'failed', reason, retry: true}) as const satisfies StatsView),
  );
  const delivering = (view: StatsView) => view.kind === 'values' && view.stale === null;

  test('a host that is delivering is never called unreachable, nor offered Reconnect, whatever its check found', () => {
    fc.assert(
      fc.property(fc.constantFrom(...REPORTS), VIEWS, (report, view) => {
        const machine = machineWith(report);
        if (!delivering(view)) return;
        expect(cardStatus(machine, view).word).not.toBe(problemWord('unavailable'));
        expect(cardAction(machine, view)).not.toBe('reconnect');
      }),
    );
  });

  test('the rail folds what the card folds: a card that says Online leaves the rail nothing to warn of, and a host adds no warning of its own', () => {
    fc.assert(
      fc.property(fc.constantFrom(...REPORTS), VIEWS, (report, view) => {
        const machine = machineWith(report);
        const state = {...ready(['ssh:host-a']), machines: [machine]};
        const reached = delivering(view) ? new Set([machine.host]) : new Set<string>();
        const attention = machinesAttention(state, reached);
        if (cardStatus(machine, view).word === 'Online') expect(attention).toBeNull();
        // What the stream knows only takes warnings away.
        expect(attention === null || attention === machinesAttention(state)).toBe(true);
        // What only the check can know is still warned of while the host delivers.
        if (machineStatus(machine) !== 'unavailable')
          expect(attention).toBe(machinesAttention(state));
      }),
    );
  });

  test('the chip and the action never contradict: an online card has nothing to fix, and a fix is never offered beside it', () => {
    fc.assert(
      fc.property(fc.constantFrom(...REPORTS), VIEWS, (report, view) => {
        const machine = machineWith(report);
        const status = cardStatus(machine, view);
        const action = cardAction(machine, view);
        if (status.word === 'Online') expect(action).toBeNull();
        // Reconnect is the fix for a connection the card says is down, and only for that.
        if (action === 'reconnect') {
          expect(delivering(view)).toBe(false);
          expect(status.word).not.toBe('Online');
        }
      }),
    );
  });

  test('what only the check can know still speaks while the host delivers: a sign-in, a missing checkout, an old version', () => {
    const live: StatsView = {kind: 'values', gauges: [], stale: null};
    for (const [report, word] of [
      [{kind: 'sign-in', detail: 'waiting'}, 'Sign in'],
      [{kind: 'checkout-not-found', detail: 'gone'}, problemWord('checkout-not-found')],
      [{kind: 'needs-update', facts: FACTS(0, 0)}, problemWord('needs-update')],
    ] as const) {
      expect(cardStatus(machineWith(report), live).word).toBe(word);
    }
  });

  test('a check that timed out is Online beside live readings, and Unreachable once the readings stop', () => {
    const timedOut = machineWith({kind: 'unavailable', detail: 'deadline exceeded after 10 s'});
    expect(cardStatus(timedOut, {kind: 'values', gauges: [], stale: null}).word).toBe('Online');
    expect(cardAction(timedOut, {kind: 'values', gauges: [], stale: null})).toBeNull();
    const stale: StatsView = {kind: 'values', gauges: [], stale: {valueAt: 1, reconnecting: false}};
    expect(cardStatus(timedOut, stale).word).toBe(problemWord('unavailable'));
    expect(cardAction(timedOut, stale)).toBe('reconnect');
    expect(cardStatus(timedOut).word).toBe(problemWord('unavailable'));
  });
});
