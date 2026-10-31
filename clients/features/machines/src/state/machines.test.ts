import {describe, expect, test} from 'bun:test';
import type {CompletionResult, MachineDescription} from '@vibesys/hosts';
import type {
  CheckoutInspection,
  ClusterTest,
  ComputeSettings,
  MachineEntry,
  MachineReport,
  Offered,
  SlurmClusterSettings,
} from '@vibesys/ipc';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import fc from 'fast-check';
import {
  type FormName,
  formSuggestions,
  initialMachines,
  type MachinesAction,
  type MachinesRequest,
  type MachinesState,
  machineCounts,
  machineFacts,
  machineStatus,
  machinesAwaited,
  machinesInvariants,
  machinesStep,
  selectedMachine,
} from './index.js';

const FACTS: MachineDescription = {
  checkout: {root: '/home/me/vibesys', version: '0.1.1', commit: 'e66d941aa', dirty: false},
  hardware: {cpu_cores: 64, gpus: [], accelerator: 'cpu'},
  docker: {reachable: true, version: '29.1'},
  harness_clis: ['claude', 'codex', 'mystery'],
  harness_cli_names: {claude: 'Claude Code', codex: 'Codex'},
  runs: {live: 1, ended_today: 2},
};

const ENTRIES: readonly MachineEntry[] = [
  {host: 'local', label: 'This Mac', checkout: '/Users/me/vibesys'},
  {host: 'ssh:host-a', label: 'host-a', checkout: '/mnt/data/vibesys'},
  {host: 'ssh:host-b', label: 'host-b', checkout: '~/vibesys'},
  {host: 'ssh:host-c', label: 'host-c', checkout: '~/vibesys'},
];
const HOSTS = [...ENTRIES.map(entry => entry.host), 'ssh:gone'];
const PATHS = ['/home/me/vibesys', '/home/me/vibesys-old', '/home/me/src'];
const FORMS: readonly FormName[] = ['add', 'fix'];

const REPORTS: readonly MachineReport[] = [
  {kind: 'ready', facts: FACTS},
  {kind: 'ready', facts: null},
  {kind: 'checkout-not-found', detail: '~/vibesys on host-b is not a directory.'},
  {kind: 'sign-in', detail: 'the SSH key needs its passphrase'},
  {kind: 'unavailable', detail: 'the host did not answer in time'},
];

const INSPECTIONS: readonly CheckoutInspection[] = [
  {
    state: 'found',
    root: '/home/me/vibesys',
    version: '0.1.1',
    commit: 'e66d941',
    dirty: false,
    compatible: true,
  },
  {state: 'too_old', version: '0.0.9', min: '0.1.0'},
  {state: 'not_found'},
  {state: 'unsupported'},
];

const CLUSTERS: readonly SlurmClusterSettings[] = [
  {
    name: 'lab-h100',
    reach: {kind: 'ssh', host: 'host-login'},
    workspace: '/scratch/vibesys',
    hardware: {accelerator: 'cuda', gpu_models: ['h100'], gpus_per_node: 8},
  },
  {name: 'campus', reach: {kind: 'login_node'}, workspace: '/work/vibesys', hardware: null},
];

function computeOf(random: Seeded): Offered<ComputeSettings> {
  if (random.next() < 0.2) return {kind: 'unsupported'};
  return {
    kind: 'offered',
    value: {
      config_path: '/home/me/.config/vibesys/compute.toml',
      clusters: CLUSTERS.filter(() => random.bool()).map(cluster => ({cluster, source: 'vibesys'})),
      modal: {token_present: random.bool()},
    },
  };
}

const TESTS: readonly ClusterTest[] = [
  {
    outcome: 'passed',
    hardware: {accelerator: 'cuda', gpu_models: ['h100'], gpus_per_node: 8},
    partitions: [],
  },
  {outcome: 'failed', stage: 'reach', detail: 'cannot reach host-login'},
];

const LISTING: CompletionResult = {
  ok: true,
  value: {
    kind: 'listed',
    directory: '/home/me/',
    entries: [
      {name: 'src', path: '/home/me/src', checkout: false, project: false},
      {name: 'vibesys', path: '/home/me/vibesys', checkout: true, project: false},
      {name: 'vibesys-old', path: '/home/me/vibesys-old', checkout: true, project: false},
    ],
    truncated: false,
  },
};

function fail<T>(error: string): {readonly ok: false; readonly error: string} & {value?: T} {
  return {ok: false, error};
}

function someEntries(random: Seeded): MachineEntry[] {
  return ENTRIES.filter(() => random.bool()).map(entry =>
    random.bool() ? entry : {...entry, checkout: '/srv/vibesys'},
  );
}

function userAction(random: Seeded, state: MachinesState): MachinesAction {
  const host = random.pick(HOSTS);
  const form = random.pick(FORMS);
  return random.pick<MachinesAction>([
    {type: 'select', host},
    {type: 'open-add'},
    {type: 'add-host', value: random.pick(['host-d', 'host-b', '-bad', ''])},
    {
      type: 'checkout',
      form,
      value: random.pick(['~/', '/home/me/', '/home/me/vib', '', 'relative']),
    },
    {type: 'submit', form},
    {type: 'close', form},
    {type: 'open-fix', host: state.selected ?? host},
    {type: 'remove', host},
    {type: 'sign-in', host},
    {type: 'reconnect', host},
    {type: 'delivering', hosts: HOSTS.filter(() => random.bool())},
    {type: 'test-cluster', host, name: random.pick(CLUSTERS).name},
  ]);
}

function replyAction(random: Seeded, token: number): MachinesAction {
  const form = random.pick(FORMS);
  const ok = random.next() < 0.8;
  return random.pick<MachinesAction>([
    {type: 'listed', token, result: ok ? {ok, value: someEntries(random)} : fail('unreadable')},
    {
      type: 'reported',
      token,
      host: random.pick(HOSTS),
      result: ok ? {ok, value: random.pick(REPORTS)} : fail('gone'),
    },
    {type: 'completed', form, token, result: ok ? LISTING : fail('path_missing')},
    {
      type: 'inspected',
      form,
      token,
      path: random.pick(PATHS),
      result: ok ? {ok, value: random.pick(INSPECTIONS)} : fail('gone'),
    },
    {
      type: 'saved',
      form,
      token,
      result: ok ? {ok, value: '/home/me/vibesys'} : fail('not a directory'),
    },
    {
      type: 'done',
      token,
      result: ok ? {ok, value: random.bool() ? null : someEntries(random)} : fail('denied'),
    },
    {
      type: 'computed',
      token,
      host: random.pick(HOSTS),
      result: ok ? {ok, value: computeOf(random)} : fail('refused'),
    },
    {
      type: 'tested',
      token,
      host: random.pick(HOSTS),
      name: random.pick(CLUSTERS).name,
      at: random.int(1000),
      result: ok ? {ok, value: random.pick(TESTS)} : fail('refused'),
    },
  ]);
}

function replySlot(action: MachinesAction): {readonly slot: string; readonly token: number} | null {
  switch (action.type) {
    case 'listed':
      return {slot: 'listing', token: action.token};
    case 'reported':
      return {slot: `report:${action.host}`, token: action.token};
    case 'completed':
      return {slot: `${action.form}:listing`, token: action.token};
    case 'inspected':
      return {slot: `${action.form}:inspect:${action.path}`, token: action.token};
    case 'saved':
      return {slot: `${action.form}:save`, token: action.token};
    case 'done':
      return {slot: 'pending', token: action.token};
    case 'computed':
      return {slot: `compute:${action.host}`, token: action.token};
    case 'tested':
      return {slot: `test:${action.host}:${action.name}`, token: action.token};
    default:
      return null;
  }
}

function issued(request: MachinesRequest): number | null {
  return request.kind === 'cancel-save' ? null : request.token;
}

/** The step properties beyond token bookkeeping: what each kind of request is allowed to say. */
function checkRequests(
  before: MachinesState,
  action: MachinesAction,
  after: MachinesState,
  requests: readonly MachinesRequest[],
): void {
  // The count the Settings navigation shows is the list the screen draws, in every state.
  const counts = machineCounts(after);
  expect(counts.count).toBe(after.machines.length);
  expect(counts.attention).toBeLessThanOrEqual(counts.count);
  for (const request of requests) {
    if (request.kind === 'save') {
      expect(action.type).toBe('submit');
      expect(request.checkout.trim()).not.toBe('');
      expect(request.host.startsWith('ssh:') || request.form === 'fix').toBe(true);
    }
    if (request.kind === 'remove') expect(request.host).not.toBe('local');
    if (request.kind === 'cancel-save') {
      const awaitedBefore = [before.adding?.saving, before.fixing?.saving];
      expect(awaitedBefore).toContain(request.token);
    }
  }
  if (action.type === 'saved' && machinesAwaited(before)[`${action.form}:save`] === action.token) {
    expect(requests.some(request => request.kind === 'list')).toBe(action.result.ok);
  }
  // A report that finds the shown machine's checkout gone opens its fix inline.
  const selected = selectedMachine(after);
  if (action.type === 'reported' && after !== before && selected?.host === action.host) {
    if (machineStatus(selected) === 'checkout-not-found') expect(after.fixing).not.toBeNull();
  }
}

describe('machinesStep', () => {
  test('model: tokens, stale replies, invariants and request properties over seeded sequences', () => {
    checkModel<MachinesState, MachinesAction, MachinesRequest>(
      {
        initial: initialMachines().state,
        step: machinesStep,
        action: (random, tokens, state) =>
          random.next() < 0.45
            ? userAction(random, state)
            : replyAction(random, random.pick(tokens)),
        issues: issued,
        awaited: machinesAwaited,
        reply: replySlot,
        invariants: machinesInvariants,
        also: checkRequests,
      },
      60,
    );
  });

  test('a machine whose checkout is gone opens its fix inline, and a saved fix checks it again', () => {
    let {state} = initialMachines();
    const run = (action: MachinesAction): readonly MachinesRequest[] => {
      const step = machinesStep(state, action);
      state = step.state;
      return step.requests;
    };
    const reports = run({
      type: 'listed',
      token: 1,
      result: {ok: true, value: [ENTRIES[2] as MachineEntry]},
    });
    const report = reports.find(request => request.kind === 'report');
    expect(report).toMatchObject({kind: 'report', host: 'ssh:host-b'});
    expect(machineStatus(state.machines[0] as never)).toBe('checking');
    run({
      type: 'reported',
      token: report?.token ?? 0,
      host: 'ssh:host-b',
      result: {ok: true, value: {kind: 'checkout-not-found', detail: 'not a directory'}},
    });
    expect(state.fixing).toMatchObject({host: 'ssh:host-b', saving: null});
    expect(state.fixing?.path.checkout).toBe('~/vibesys');
    run({type: 'checkout', form: 'fix', value: '/home/me/src/vibesys'});
    const save = run({type: 'submit', form: 'fix'}).find(request => request.kind === 'save');
    expect(save).toMatchObject({host: 'ssh:host-b', checkout: '/home/me/src/vibesys'});
    const relist = run({
      type: 'saved',
      form: 'fix',
      token: save?.token ?? 0,
      result: {ok: true, value: '/home/me/src/vibesys'},
    });
    expect(state.fixing).toBeNull();
    expect(relist).toEqual([{kind: 'list', token: state.listing?.token ?? 0}]);
    expect(state.listing?.select).toBe('ssh:host-b');
  });

  test('regression: the Machines count follows the list when a machine is added or removed', () => {
    let {state} = initialMachines();
    const run = (action: MachinesAction): void => {
      state = machinesStep(state, action).state;
    };
    run({type: 'listed', token: 1, result: {ok: true, value: ENTRIES.slice(0, 2)}});
    expect(machineCounts(state)).toEqual({count: 2, attention: 0, listed: true});
    // A machine is added: the screen lists again, and the count is the new list's.
    const token = state.lastToken + 1;
    state = {...state, listing: {token, select: null}, lastToken: token};
    run({type: 'listed', token, result: {ok: true, value: ENTRIES.slice(0, 3)}});
    expect(machineCounts(state)).toEqual({count: 3, attention: 0, listed: true});
    const host = state.machines[2]?.host ?? 'local';
    const checking = state.machines.find(machine => machine.host === host)?.checking ?? 0;
    run({
      type: 'reported',
      token: checking,
      host,
      result: {ok: true, value: {kind: 'sign-in', detail: 'the key needs its passphrase'}},
    });
    expect(machineCounts(state)).toEqual({count: 3, attention: 1, listed: true});
  });

  test('regression: no count is worded while the machines are being read or could not be', () => {
    const {state: reading} = initialMachines();
    expect(machineCounts(reading)).toEqual({count: 0, attention: 0, listed: false});

    const failed = machinesStep(reading, {
      type: 'listed',
      token: 1,
      result: {ok: false, error: 'hosts.json is unreadable.'},
    }).state;
    expect(machineCounts(failed)).toEqual({count: 0, attention: 0, listed: false});

    const empty = machinesStep(reading, {type: 'listed', token: 1, result: {ok: true, value: []}});
    expect(machineCounts(empty.state)).toEqual({count: 0, attention: 0, listed: true});
  });

  test('regression: a sign-in awaited for a machine the next listing drops is forgotten', () => {
    let {state} = initialMachines();
    const run = (action: MachinesAction): void => {
      state = machinesStep(state, action).state;
    };
    run({type: 'listed', token: 1, result: {ok: true, value: ENTRIES.slice(0, 2)}});
    run({type: 'sign-in', host: 'ssh:host-a'});
    expect(state.pending).toMatchObject({kind: 'sign-in', host: 'ssh:host-a'});
    const token = state.lastToken + 1;
    state = {...state, listing: {token, select: null}, lastToken: token};
    run({type: 'listed', token, result: {ok: true, value: ENTRIES.slice(0, 1)}});
    expect(state.pending).toBeNull();
    expect(machinesInvariants(state)).toEqual([]);
  });

  test('checkout save and removal for one machine are mutually exclusive', () => {
    const listed = machinesStep(initialMachines().state, {
      type: 'listed',
      token: 1,
      result: {ok: true, value: [ENTRIES[2] as MachineEntry]},
    }).state;
    const fixing = machinesStep(listed, {type: 'open-fix', host: 'ssh:host-b'}).state;

    const saving = machinesStep(fixing, {type: 'submit', form: 'fix'});
    expect(saving.requests).toContainEqual(
      expect.objectContaining({kind: 'save', host: 'ssh:host-b'}),
    );
    const removeDuringSave = machinesStep(saving.state, {
      type: 'remove',
      host: 'ssh:host-b',
    });
    expect(removeDuringSave.state).toBe(saving.state);
    expect(removeDuringSave.requests).toEqual([]);

    const removing = machinesStep(fixing, {type: 'remove', host: 'ssh:host-b'});
    expect(removing.requests).toContainEqual(
      expect.objectContaining({kind: 'remove', host: 'ssh:host-b'}),
    );
    const saveDuringRemove = machinesStep(removing.state, {type: 'submit', form: 'fix'});
    expect(saveDuringRemove.state).toBe(removing.state);
    expect(saveDuringRemove.requests).toEqual([]);
  });

  test('Add refuses a host or path it cannot use, naming the problem, and asks nothing', () => {
    const listed = machinesStep(initialMachines().state, {
      type: 'listed',
      token: 1,
      result: {ok: true, value: [ENTRIES[2] as MachineEntry]},
    }).state;
    const cases: readonly [string, string, 'host' | 'checkout', string][] = [
      ['-oProxyCommand=x', '~/vibesys', 'host', 'is not a usable ssh host name'],
      ['host-b', '~/vibesys', 'host', 'already a machine'],
      ['', '', 'host', 'The SSH host is required.'],
      ['', '~/vibesys', 'host', 'The SSH host is required.'],
      ['host-d', 'vibesys', 'checkout', 'must be an absolute path or start with ~/'],
      ['host-d', '', 'checkout', 'The VibeSys checkout is required.'],
    ];
    for (const [host, checkout, field, problem] of cases) {
      let state = machinesStep(listed, {type: 'open-add'}).state;
      state = machinesStep(state, {type: 'add-host', value: host}).state;
      state = machinesStep(state, {type: 'checkout', form: 'add', value: checkout}).state;
      const step = machinesStep(state, {type: 'submit', form: 'add'});
      expect(step.requests).toEqual([]);
      expect(step.state.adding?.problem?.field).toBe(field);
      expect(step.state.adding?.problem?.text).toContain(problem);
    }
  });

  test('checkout suggestions carry the version each checkout reports', () => {
    let state = machinesStep(initialMachines().state, {type: 'open-add'}).state;
    let requests: readonly MachinesRequest[] = [];
    for (const action of [
      {type: 'add-host', value: 'host-c'},
      {type: 'checkout', form: 'add', value: '/home/me/'},
    ] as const) {
      const step = machinesStep(state, action);
      state = step.state;
      requests = step.requests;
    }
    const listing = requests.find(request => request.kind === 'complete');
    expect(listing).toMatchObject({host: 'ssh:host-c', directory: '/home/me/', form: 'add'});
    const asked = machinesStep(state, {
      type: 'completed',
      form: 'add',
      token: listing?.token ?? 0,
      result: LISTING,
    });
    state = asked.state;
    const inspections = asked.requests.filter(request => request.kind === 'inspect');
    expect(inspections.map(request => request.kind === 'inspect' && request.path)).toEqual([
      '/home/me/vibesys',
      '/home/me/vibesys-old',
    ]);
    for (const [request, inspection] of inspections.map(
      (request, index) => [request, INSPECTIONS[index]] as const,
    )) {
      if (request.kind !== 'inspect' || inspection === undefined) continue;
      state = machinesStep(state, {
        type: 'inspected',
        form: 'add',
        path: request.path,
        token: request.token,
        result: {ok: true, value: inspection},
      }).state;
    }
    expect(formSuggestions(state.adding?.path as never)).toEqual([
      {value: '/home/me/src/'},
      {value: '/home/me/vibesys/', detail: '0.1.1 · e66d941'},
      {value: '/home/me/vibesys-old/', detail: '0.0.9 · too old for this app'},
    ]);
  });

  test('a ready machine lists its facts', () => {
    const step = machinesStep(initialMachines().state, {
      type: 'listed',
      token: 1,
      result: {ok: true, value: [ENTRIES[2] as MachineEntry]},
    });
    const token = step.requests[0]?.token ?? 0;
    const state = machinesStep(step.state, {
      type: 'reported',
      token,
      host: 'ssh:host-b',
      result: {ok: true, value: {kind: 'ready', facts: FACTS}},
    }).state;
    expect(machineFacts(state.machines[0] as never)).toEqual([
      {key: 'Checkout', value: '~/vibesys', mono: true},
      {key: 'Version', value: '0.1.1 · e66d941 · clean', mono: true},
      {key: 'Hardware', value: '64 CPU cores · no GPU', mono: false},
      {key: 'Docker', value: 'Docker 29.1', mono: false},
      {key: 'Harnesses', value: 'Claude Code · Codex · mystery installed', mono: false},
      {key: 'Runs', value: '1 live · 2 ended today', mono: false},
    ]);
  });
});

describe('a host that streams though its check found it unreachable', () => {
  const NAMES = ['ssh:host-a', 'ssh:host-b', 'ssh:host-c'];
  const UNREACHABLE: MachineReport = {kind: 'unavailable', detail: 'timed out'};

  /** Machines each listed and each checked, with the check `report` of each by host. */
  function checked(reports: Readonly<Record<string, MachineReport>>): MachinesState {
    let {state} = initialMachines();
    const entries = NAMES.map(host => ({host, label: host, checkout: '~/vibesys'}));
    state = machinesStep(state, {
      type: 'listed',
      token: 1,
      result: {ok: true, value: entries},
    }).state;
    for (const machine of state.machines) {
      state = machinesStep(state, {
        type: 'reported',
        token: machine.checking ?? 0,
        host: machine.host,
        result: {ok: true, value: reports[machine.host] ?? {kind: 'ready', facts: FACTS}},
      }).state;
    }
    return state;
  }

  /** The runs of consecutive sets that hold `host`: each is a spell of delivering. */
  function spells(sets: readonly (readonly string[])[], host: string): number {
    return sets.filter((set, index) => set.includes(host) && !sets[index - 1]?.includes(host))
      .length;
  }

  /** Plays `sets` as the hosts that deliver, answering each check with the host unreachable again. */
  function play(start: MachinesState, sets: readonly (readonly string[])[]): Map<string, number> {
    const asked = new Map<string, number>();
    let state = start;
    for (const set of sets) {
      const step = machinesStep(state, {type: 'delivering', hosts: set});
      state = step.state;
      for (const request of step.requests) {
        if (request.kind !== 'report') throw new Error(`asked for ${request.kind}`);
        asked.set(request.host, (asked.get(request.host) ?? 0) + 1);
        const result = {ok: true, value: UNREACHABLE} as const;
        state = machinesStep(state, {
          type: 'reported',
          token: request.token,
          host: request.host,
          result,
        }).state;
      }
    }
    return asked;
  }

  test('property: it is checked again once for each spell of delivering, and only if its check failed', () => {
    fc.assert(
      fc.property(
        fc.array(fc.subarray(NAMES), {maxLength: 30}),
        fc.subarray(NAMES),
        (sets, unreachable) => {
          const start = checked(
            Object.fromEntries(unreachable.map(host => [host, UNREACHABLE] as const)),
          );
          const asked = play(start, sets);
          for (const host of NAMES) {
            expect(asked.get(host) ?? 0).toBe(unreachable.includes(host) ? spells(sets, host) : 0);
          }
        },
      ),
    );
  });

  test('a check that cannot start is not owed, and is asked for when the host delivers again', () => {
    const base = checked({'ssh:host-a': UNREACHABLE});
    const waiting = machinesStep(base, {type: 'remove', host: 'ssh:host-b'}).state;
    // A removal is awaited: no check starts, and none is counted as asked.
    const blocked = machinesStep(waiting, {type: 'delivering', hosts: ['ssh:host-a']});
    expect(blocked.requests).toEqual([]);
    expect(blocked.state.rechecked).toEqual([]);
    const free = machinesStep(base, {type: 'delivering', hosts: ['ssh:host-a']});
    expect(free.requests).toEqual([expect.objectContaining({kind: 'report', host: 'ssh:host-a'})]);
    expect(free.state.rechecked).toEqual(['ssh:host-a']);
    // Hearing the same set again changes nothing, not even the state's identity.
    const again = machinesStep(free.state, {type: 'delivering', hosts: ['ssh:host-a']});
    expect(again.state).toBe(free.state);
    expect(again.requests).toEqual([]);
  });
});
