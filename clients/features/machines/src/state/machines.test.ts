import {describe, expect, test} from 'bun:test';
import type {CompletionResult, MachineDescription} from '@vibesys/hosts';
import type {CheckoutInspection, MachineEntry, MachineReport} from '@vibesys/ipc';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  type FormName,
  formSuggestions,
  initialMachines,
  type MachinesAction,
  type MachinesRequest,
  type MachinesState,
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
  harness_clis: ['claude', 'codex'],
  runs: {live: 1, ended_today: 2},
};

const ENTRIES: readonly MachineEntry[] = [
  {host: 'local', label: 'This Mac', checkout: '/Users/me/vibesys'},
  {host: 'ssh:chelan3', label: 'chelan3', checkout: '/mnt/data/vibesys'},
  {host: 'ssh:chelan4', label: 'chelan4', checkout: '~/vibesys'},
  {host: 'ssh:chelan5', label: 'chelan5', checkout: '~/vibesys'},
];
const HOSTS = [...ENTRIES.map(entry => entry.host), 'ssh:gone'];
const PATHS = ['/home/me/vibesys', '/home/me/vibesys-old', '/home/me/src'];
const FORMS: readonly FormName[] = ['add', 'fix'];

const REPORTS: readonly MachineReport[] = [
  {kind: 'ready', facts: FACTS},
  {kind: 'ready', facts: null},
  {kind: 'checkout-not-found', detail: '~/vibesys on chelan4 is not a directory.'},
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
    {type: 'add-host', value: random.pick(['chelan6', 'chelan4', '-bad', ''])},
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
    expect(report).toMatchObject({kind: 'report', host: 'ssh:chelan4'});
    expect(machineStatus(state.machines[0] as never)).toBe('checking');
    run({
      type: 'reported',
      token: report?.token ?? 0,
      host: 'ssh:chelan4',
      result: {ok: true, value: {kind: 'checkout-not-found', detail: 'not a directory'}},
    });
    expect(state.fixing).toMatchObject({host: 'ssh:chelan4', saving: null});
    expect(state.fixing?.path.checkout).toBe('~/vibesys');
    run({type: 'checkout', form: 'fix', value: '/home/me/src/vibesys'});
    const save = run({type: 'submit', form: 'fix'}).find(request => request.kind === 'save');
    expect(save).toMatchObject({host: 'ssh:chelan4', checkout: '/home/me/src/vibesys'});
    const relist = run({
      type: 'saved',
      form: 'fix',
      token: save?.token ?? 0,
      result: {ok: true, value: '/home/me/src/vibesys'},
    });
    expect(state.fixing).toBeNull();
    expect(relist).toEqual([{kind: 'list', token: state.listing?.token ?? 0}]);
    expect(state.listing?.select).toBe('ssh:chelan4');
  });

  test('Add refuses a host or path it cannot use, naming the problem, and asks nothing', () => {
    const listed = machinesStep(initialMachines().state, {
      type: 'listed',
      token: 1,
      result: {ok: true, value: [ENTRIES[2] as MachineEntry]},
    }).state;
    const cases: readonly [string, string, string][] = [
      ['-oProxyCommand=x', '~/vibesys', 'is not a usable ssh host name'],
      ['chelan4', '~/vibesys', 'already a machine'],
      ['chelan6', 'vibesys', 'must be an absolute path or start with ~/'],
      ['chelan6', '', 'must not be empty'],
    ];
    for (const [host, checkout, problem] of cases) {
      let state = machinesStep(listed, {type: 'open-add'}).state;
      state = machinesStep(state, {type: 'add-host', value: host}).state;
      state = machinesStep(state, {type: 'checkout', form: 'add', value: checkout}).state;
      const step = machinesStep(state, {type: 'submit', form: 'add'});
      expect(step.requests).toEqual([]);
      expect(step.state.adding?.problem).toContain(problem);
    }
  });

  test('checkout suggestions carry the version each checkout reports', () => {
    let state = machinesStep(initialMachines().state, {type: 'open-add'}).state;
    let requests: readonly MachinesRequest[] = [];
    for (const action of [
      {type: 'add-host', value: 'chelan5'},
      {type: 'checkout', form: 'add', value: '/home/me/'},
    ] as const) {
      const step = machinesStep(state, action);
      state = step.state;
      requests = step.requests;
    }
    const listing = requests.find(request => request.kind === 'complete');
    expect(listing).toMatchObject({host: 'ssh:chelan5', directory: '/home/me/', form: 'add'});
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
      host: 'ssh:chelan4',
      result: {ok: true, value: {kind: 'ready', facts: FACTS}},
    }).state;
    expect(machineFacts(state.machines[0] as never)).toEqual([
      {key: 'Checkout', value: '~/vibesys', mono: true},
      {key: 'Version', value: '0.1.1 · e66d941 · clean', mono: true},
      {key: 'Hardware', value: '64 CPU cores · no GPU', mono: false},
      {key: 'Docker', value: 'docker 29.1', mono: false},
      {key: 'Harnesses', value: 'Claude · Codex installed', mono: false},
      {key: 'Runs', value: '1 live · 2 ended today', mono: false},
    ]);
  });
});
