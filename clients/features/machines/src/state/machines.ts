/**
 * The Machines screen's store: a pure reducer over the saved machines, what each one's facts say,
 * the selection, the Add form and the checkout form (the inline fix for a missing checkout, and
 * Change checkout). A machine's status is a selector over its last report, never stored on its own.
 * Every request carries a token from the state's one counter and the state that awaits it keeps
 * it, so a late reply (a report for a machine re-checked since, a listing of a directory the user
 * left, a save the user cancelled) is dropped.
 */
import {
  type CompletionResult,
  type HostKey,
  hostKey,
  type MachineStatus,
  validAlias,
  validHostPath,
} from '@vibesys/hosts';
import type {CheckoutInspection, FailureCause, MachineEntry, MachineReport} from '@vibesys/ipc';
import {
  formAwaited,
  inspectedReply,
  listed,
  type PathForm,
  type PathFormRequest,
  type PathFormStep,
  pathForm,
  typed,
} from './path-form.js';
import {
  keepPlaces,
  type Places,
  type PlacesAction,
  type PlacesRequest,
  type PlacesState,
  type PlacesStep,
  placesAwaited,
  placesStep,
  readPlaces,
} from './places.js';

/** A machine's status, from its facts: `@vibesys/hosts`' one vocabulary, a report's kinds and checking. */
export type {MachineStatus};

export interface Machine extends MachineEntry {
  /** What its facts said last, or null before the first check answered. */
  readonly report: MachineReport | null;
  /** The check awaited, or null. */
  readonly checking: number | null;
}

/** Which form a path field belongs to. */
export type FormName = 'add' | 'fix';

/** Which field of a form a problem belongs under. */
type FormField = 'host' | 'checkout';

/** A removal or sign-in that failed: for which machine, and what it reported. */
export interface ActionProblem {
  readonly kind: 'remove' | 'sign-in';
  readonly host: HostKey;
  /** The attempt's token, so a failure that comes again is a new problem, not the dismissed one. */
  readonly token: number;
  /** What the machine reported, in its own words. */
  readonly error: string;
}

/** What is wrong with a form, and the one field it names. */
interface FormProblem {
  readonly field: FormField;
  readonly text: string;
  /** Set when the connection failed: the screen words it as a status and keeps `text` for the pointer. */
  readonly cause?: FailureCause;
}

interface AddForm {
  /** The SSH host as typed. */
  readonly host: string;
  readonly path: PathForm;
  /** The check and save awaited, or null while the user edits. */
  readonly saving: number | null;
  /** Why the last check failed, or what is wrong with the input; null when nothing is. */
  readonly problem: FormProblem | null;
}

interface FixForm {
  readonly host: HostKey;
  readonly path: PathForm;
  readonly saving: number | null;
  readonly problem: FormProblem | null;
}

export interface MachinesState {
  /**
   * The listing awaited, and the machine to select once it is in (one just added or fixed), or
   * null once the machines are listed.
   */
  readonly listing: {readonly token: number; readonly select: HostKey | null} | null;
  /** Why the machines could not be listed, until they are. */
  readonly listProblem: string | null;
  readonly machines: readonly Machine[];
  readonly selected: HostKey | null;
  readonly adding: AddForm | null;
  readonly fixing: FixForm | null;
  /** A removal or a sign-in awaited, by its machine. */
  readonly pending: {
    readonly kind: 'remove' | 'sign-in';
    readonly host: HostKey;
    readonly token: number;
  } | null;
  /** The last removal or sign-in that failed, for its machine, until an action succeeds. */
  readonly actionProblem: ActionProblem | null;
  /** What each ready machine has set up and how its clusters tested, by machine. */
  readonly places: Readonly<Record<HostKey, Places>>;
  /**
   * The machines checked again because their host streams though their check found them
   * unreachable, for as long as they keep streaming: one more check per spell of streaming.
   */
  readonly rechecked: readonly HostKey[];
  readonly lastToken: number;
}

type Outcome<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly cause?: FailureCause};

export type MachinesAction =
  | PlacesAction
  /** Check the machine again, signing in first when it is waiting for that. */
  | {readonly type: 'reconnect'; readonly host: HostKey}
  /** The hosts whose stream delivers now: each one whose check failed is checked again, once. */
  | {readonly type: 'delivering'; readonly hosts: readonly HostKey[]}
  | {
      readonly type: 'listed';
      readonly token: number;
      readonly result: Outcome<readonly MachineEntry[]>;
    }
  | {
      readonly type: 'reported';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<MachineReport>;
    }
  | {readonly type: 'select'; readonly host: HostKey}
  | {readonly type: 'open-add'}
  | {readonly type: 'add-host'; readonly value: string}
  | {readonly type: 'checkout'; readonly form: FormName; readonly value: string}
  | {readonly type: 'submit'; readonly form: FormName}
  | {readonly type: 'close'; readonly form: FormName}
  | {readonly type: 'open-fix'; readonly host: HostKey}
  | {
      readonly type: 'completed';
      readonly form: FormName;
      readonly token: number;
      readonly result: CompletionResult;
    }
  | {
      readonly type: 'inspected';
      readonly form: FormName;
      readonly path: string;
      readonly token: number;
      readonly result: Outcome<CheckoutInspection>;
    }
  | {
      readonly type: 'saved';
      readonly form: FormName;
      readonly token: number;
      readonly result: Outcome<string>;
    }
  | {readonly type: 'remove'; readonly host: HostKey}
  | {readonly type: 'sign-in'; readonly host: HostKey}
  | {
      readonly type: 'done';
      readonly token: number;
      readonly result: Outcome<readonly MachineEntry[] | null>;
    };

export type MachinesRequest =
  | PlacesRequest
  | {readonly kind: 'list'; readonly token: number}
  | {readonly kind: 'report'; readonly token: number; readonly host: HostKey}
  | (PathFormRequest & {readonly form: FormName})
  | {
      readonly kind: 'save';
      readonly form: FormName;
      readonly token: number;
      readonly host: HostKey;
      readonly checkout: string;
    }
  | {readonly kind: 'cancel-save'; readonly token: number}
  | {readonly kind: 'remove'; readonly token: number; readonly host: HostKey}
  | {readonly kind: 'sign-in'; readonly token: number; readonly host: HostKey};

export interface MachinesStep {
  readonly state: MachinesState;
  readonly requests: readonly MachinesRequest[];
}

const same = (state: MachinesState): MachinesStep => ({state, requests: []});

const placesOf = (state: MachinesState): PlacesState => ({
  places: state.places,
  lastToken: state.lastToken,
});

function fromPlaces(state: MachinesState, step: PlacesStep): MachinesStep {
  // A reply that changed nothing leaves the very state, so a late one is seen to have done nothing.
  if (step.state.places === state.places && step.state.lastToken === state.lastToken) {
    return {state, requests: step.requests};
  }
  return {
    state: {...state, places: step.state.places, lastToken: step.state.lastToken},
    requests: step.requests,
  };
}

/** The store before anything is read: it asks for the machines. */
export function initialMachines(): MachinesStep {
  const state: MachinesState = {
    listing: {token: 1, select: null},
    listProblem: null,
    machines: [],
    selected: null,
    adding: null,
    fixing: null,
    pending: null,
    actionProblem: null,
    places: {},
    rechecked: [],
    lastToken: 1,
  };
  return {state, requests: [{kind: 'list', token: 1}]};
}

/** A machine's status now. */
export function machineStatus(machine: Machine): MachineStatus {
  return machine.checking !== null || machine.report === null ? 'checking' : machine.report.kind;
}

/**
 * The machines as the Settings navigation counts them: every machine this screen lists, and those
 * whose facts say they need attention (checked, and not ready). One list, so the count cannot
 * disagree with it. `listed` is false while the machines are being read or could not be: the
 * count is then not known, and must not be worded as "none".
 */
export function machineCounts(state: MachinesState): {
  readonly count: number;
  readonly attention: number;
  readonly listed: boolean;
} {
  const attention = state.machines.filter(machine => {
    const status = machineStatus(machine);
    return status !== 'ready' && status !== 'checking';
  });
  return {
    count: state.machines.length,
    attention: attention.length,
    listed: state.listing === null && state.listProblem === null,
  };
}

/** The machine shown in the detail, or null when there is none. */
export function selectedMachine(state: MachinesState): Machine | null {
  return state.machines.find(machine => machine.host === state.selected) ?? null;
}

function relist(state: MachinesState, select: HostKey): MachinesStep {
  const token = state.lastToken + 1;
  return {
    state: {...state, listing: {token, select}, lastToken: token},
    requests: [{kind: 'list', token}],
  };
}

/** The listed machines, each re-checked, keeping what was known about the ones that stay. */
function adoptList(
  state: MachinesState,
  entries: readonly MachineEntry[],
  select: HostKey | null,
): MachinesStep {
  let last = state.lastToken;
  const requests: MachinesRequest[] = [];
  const machines = entries.map(entry => {
    const known = state.machines.find(machine => machine.host === entry.host);
    last += 1;
    requests.push({kind: 'report', token: last, host: entry.host});
    const report = known?.checkout === entry.checkout ? known.report : null;
    return {...entry, report, checking: last};
  });
  const wanted = select ?? state.selected;
  const selected = machines.some(machine => machine.host === wanted)
    ? wanted
    : (machines[0]?.host ?? null);
  const kept = machines.some(machine => machine.host === state.fixing?.host);
  const fixing = kept ? state.fixing : null;
  const dropped = kept ? null : (state.fixing?.saving ?? null);
  return {
    state: {
      ...state,
      listing: null,
      listProblem: null,
      machines,
      selected,
      fixing,
      // A sign-in or removal for a machine the list no longer holds has nothing left to do.
      pending: machines.some(machine => machine.host === state.pending?.host)
        ? state.pending
        : null,
      places: keepPlaces(
        {places: state.places, lastToken: last},
        new Set(machines.map(machine => machine.host)),
      ).places,
      lastToken: last,
    },
    requests: dropped === null ? requests : [...requests, {kind: 'cancel-save', token: dropped}],
  };
}

function withForm(
  state: MachinesState,
  form: FormName,
  path: PathForm,
  problem?: FormProblem | null,
): MachinesState {
  if (form === 'add') {
    if (state.adding === null) return state;
    return {
      ...state,
      adding: {
        ...state.adding,
        path,
        problem: problem === undefined ? state.adding.problem : problem,
      },
    };
  }
  if (state.fixing === null) return state;
  return {
    ...state,
    fixing: {
      ...state.fixing,
      path,
      problem: problem === undefined ? state.fixing.problem : problem,
    },
  };
}

function formPath(state: MachinesState, form: FormName): PathForm | null {
  return (form === 'add' ? state.adding?.path : state.fixing?.path) ?? null;
}

function fromForm(
  state: MachinesState,
  form: FormName,
  step: PathFormStep,
  problem?: FormProblem | null,
): MachinesStep {
  return {
    state: {...withForm(state, form, step.form, problem), lastToken: step.lastToken},
    requests: step.requests.map(request => ({...request, form})),
  };
}

/** The host an Add form's text names, or null while it names none. */
function addedHost(text: string): HostKey | null {
  try {
    return hostKey({kind: 'ssh', alias: validAlias(text.trim())});
  } catch {
    return null;
  }
}

function problemOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The first thing wrong with a submitted form, under the field that names it, or the save to ask for. */
function checked(
  state: MachinesState,
  form: FormName,
  current: AddForm | FixForm,
): {readonly host: HostKey; readonly checkout: string} | FormProblem {
  let host: HostKey;
  if (form === 'add') {
    const typedHost = (state.adding?.host ?? '').trim();
    if (typedHost === '') return {field: 'host', text: 'The SSH host is required.'};
    try {
      host = hostKey({kind: 'ssh', alias: validAlias(typedHost)});
    } catch (error) {
      return {field: 'host', text: problemOf(error)};
    }
    if (state.machines.some(machine => machine.host === host)) {
      return {
        field: 'host',
        text: `${typedHost} is already a machine; change its checkout instead.`,
      };
    }
  } else {
    host = state.fixing?.host as HostKey;
  }
  if (current.path.checkout.trim() === '') {
    return {field: 'checkout', text: 'The VibeSys checkout is required.'};
  }
  try {
    return {host, checkout: validHostPath(current.path.checkout, 'the VibeSys checkout')};
  } catch (error) {
    return {field: 'checkout', text: problemOf(error)};
  }
}

function submit(state: MachinesState, form: FormName): MachinesStep {
  const current = form === 'add' ? state.adding : state.fixing;
  if (current === null || current.saving !== null || state.pending !== null) return same(state);
  const result = checked(state, form, current);
  if ('field' in result) return same(withForm(state, form, current.path, result));
  const {host, checkout} = result;
  const token = state.lastToken + 1;
  const saving = {...current, saving: token, problem: null};
  const next =
    form === 'add' ? {...state, adding: saving as AddForm} : {...state, fixing: saving as FixForm};
  return {
    state: {...next, lastToken: token},
    requests: [{kind: 'save', form, token, host, checkout}],
  };
}

function close(state: MachinesState, form: FormName): MachinesStep {
  const current = form === 'add' ? state.adding : state.fixing;
  if (current === null) return same(state);
  const next = form === 'add' ? {...state, adding: null} : {...state, fixing: null};
  return {
    state: next,
    requests: current.saving === null ? [] : [{kind: 'cancel-save', token: current.saving}],
  };
}

function saved(
  state: MachinesState,
  action: Extract<MachinesAction, {type: 'saved'}>,
): MachinesStep {
  const current = action.form === 'add' ? state.adding : state.fixing;
  if (current === null || current.saving !== action.token) return same(state);
  if (!action.result.ok) {
    const {error, cause} = action.result;
    // A failed connection is the host's, not the checkout's: it goes under the host in the Add form.
    const field = cause !== undefined && action.form === 'add' ? 'host' : 'checkout';
    const failed = {
      ...current,
      saving: null,
      problem: {field, text: error, ...(cause === undefined ? {} : {cause})},
    };
    return same(
      action.form === 'add'
        ? {...state, adding: failed as AddForm}
        : {...state, fixing: failed as FixForm},
    );
  }
  const host =
    action.form === 'add'
      ? (addedHost(state.adding?.host ?? '') as HostKey)
      : (state.fixing?.host as HostKey);
  const closed = action.form === 'add' ? {...state, adding: null} : {...state, fixing: null};
  return relist(closed, host);
}

function reported(
  state: MachinesState,
  action: Extract<MachinesAction, {type: 'reported'}>,
): MachinesStep {
  const machine = state.machines.find(each => each.host === action.host);
  if (machine === undefined || machine.checking !== action.token) return same(state);
  const report: MachineReport = action.result.ok
    ? action.result.value
    : {kind: 'unavailable', detail: action.result.error};
  const machines = state.machines.map(each =>
    each === machine ? {...each, report, checking: null} : each,
  );
  const next = {...state, machines};
  // A machine that answers can say what it has set up: read it again with each answer.
  if (report.kind !== 'ready') return same(next);
  return fromPlaces(next, readPlaces(placesOf(next), action.host));
}

/** Check `host` again, or sign it in first when it is waiting for that. */
function reconnect(state: MachinesState, host: HostKey): MachinesStep {
  const machine = state.machines.find(each => each.host === host);
  if (machine === undefined || machine.checking !== null || state.pending !== null) {
    return same(state);
  }
  if (machineStatus(machine) === 'sign-in') return startPending(state, 'sign-in', host);
  const token = state.lastToken + 1;
  const machines = state.machines.map(each =>
    each === machine ? {...each, checking: token} : each,
  );
  return {state: {...state, machines, lastToken: token}, requests: [{kind: 'report', token, host}]};
}

/**
 * The hosts to check again: those that deliver readings though their last check found them
 * unreachable (the check was wrong, or the host has come back), and have not been asked since they
 * began delivering. One more check per spell of delivering, so a check that fails again is not
 * repeated for as long as the host delivers.
 */
export function hostsToRecheck(
  machines: readonly Machine[],
  delivering: ReadonlySet<HostKey>,
  asked: ReadonlySet<HostKey>,
): readonly HostKey[] {
  return machines
    .filter(
      each =>
        machineStatus(each) === 'unavailable' && delivering.has(each.host) && !asked.has(each.host),
    )
    .map(each => each.host);
}

function delivering(state: MachinesState, hosts: readonly HostKey[]): MachinesStep {
  const reached = new Set(hosts);
  // A host that stopped delivering starts a new spell when it delivers again.
  const asked = state.rechecked.filter(host => reached.has(host));
  let next: MachinesState = {...state, rechecked: asked};
  const requests: MachinesStep['requests'][number][] = [];
  for (const host of hostsToRecheck(state.machines, reached, new Set(asked))) {
    const step = reconnect(next, host);
    // A check that could not start (one is under way, or a removal is awaited) is not owed.
    if (step.requests.length === 0) continue;
    next = {...step.state, rechecked: [...next.rechecked, host]};
    requests.push(...step.requests);
  }
  const unchanged =
    requests.length === 0 &&
    next.rechecked.length === state.rechecked.length &&
    next.rechecked.every((host, index) => host === state.rechecked[index]);
  return unchanged ? same(state) : {state: next, requests};
}

function startPending(
  state: MachinesState,
  kind: 'remove' | 'sign-in',
  host: HostKey,
): MachinesStep {
  if (state.pending !== null || !state.machines.some(machine => machine.host === host))
    return same(state);
  if (kind === 'remove' && host === hostKey({kind: 'local'})) return same(state);
  if (kind === 'remove' && state.fixing?.host === host && state.fixing.saving !== null)
    return same(state);
  const token = state.lastToken + 1;
  return {
    state: {...state, pending: {kind, host, token}, lastToken: token},
    requests: [{kind, token, host}],
  };
}

function done(state: MachinesState, action: Extract<MachinesAction, {type: 'done'}>): MachinesStep {
  const {pending} = state;
  if (pending === null || pending.token !== action.token) return same(state);
  if (!action.result.ok) {
    const {kind, host, token} = pending;
    return same({
      ...state,
      pending: null,
      actionProblem: {kind, host, token, error: action.result.error},
    });
  }
  // An action that took ends the last one's failure.
  const cleared = {...state, pending: null, actionProblem: null};
  if (pending.kind === 'remove') {
    const entries = (action.result.value ?? state.machines).filter(
      entry => entry.host !== pending.host,
    );
    const kept = entries.flatMap(entry => {
      const known = state.machines.find(machine => machine.host === entry.host);
      return known === undefined ? [] : [{...known, ...entry}];
    });
    const listed = kept.some(machine => machine.host === cleared.fixing?.host);
    const fixing = listed ? cleared.fixing : null;
    const dropped = listed ? null : (cleared.fixing?.saving ?? null);
    const selected = kept.some(machine => machine.host === state.selected)
      ? state.selected
      : (kept[0]?.host ?? null);
    const places = keepPlaces(placesOf(cleared), new Set(kept.map(machine => machine.host)));
    return {
      state: {...cleared, fixing, machines: kept, selected, places: places.places},
      requests: dropped === null ? [] : [{kind: 'cancel-save', token: dropped}],
    };
  }
  // Signed in: check the machine again.
  const token = state.lastToken + 1;
  const machines = state.machines.map(machine =>
    machine.host === pending.host ? {...machine, checking: token} : machine,
  );
  return {
    state: {...cleared, machines, lastToken: token},
    requests: [{kind: 'report', token, host: pending.host}],
  };
}

function openFix(state: MachinesState, host: HostKey): MachinesStep {
  const machine = state.machines.find(each => each.host === host);
  if (machine === undefined) return same(state);
  const closing = close(state, 'fix');
  const step = pathForm(host, machine.checkout, closing.state.lastToken);
  const fixing: FixForm = {host, path: step.form, saving: null, problem: null};
  return {
    state: {...closing.state, fixing, selected: host, lastToken: step.lastToken},
    requests: [
      ...closing.requests,
      ...step.requests.map(request => ({...request, form: 'fix' as const})),
    ],
  };
}

function formReply(
  state: MachinesState,
  action: Extract<MachinesAction, {type: 'completed' | 'inspected'}>,
): MachinesStep {
  const path = formPath(state, action.form);
  if (path === null) return same(state);
  if (action.type === 'completed') {
    const step = listed(path, action.token, action.result, state.lastToken);
    return step === null ? same(state) : fromForm(state, action.form, step);
  }
  const inspection = action.result.ok ? action.result.value : null;
  const next = inspectedReply(path, action.path, action.token, inspection);
  return next === null ? same(state) : same(withForm(state, action.form, next));
}

function edit(
  state: MachinesState,
  action: Extract<MachinesAction, {type: 'open-add' | 'add-host' | 'checkout'}>,
): MachinesStep {
  switch (action.type) {
    case 'open-add': {
      if (state.adding !== null) return same(state);
      const step = pathForm(null, '', state.lastToken);
      return {
        state: {
          ...state,
          adding: {host: '', path: step.form, saving: null, problem: null},
          lastToken: step.lastToken,
        },
        requests: [],
      };
    }
    case 'add-host': {
      if (state.adding === null || state.adding.saving !== null) return same(state);
      const host = addedHost(action.value);
      const step = typed({...state.adding.path, host}, state.adding.path.checkout, state.lastToken);
      return fromForm({...state, adding: {...state.adding, host: action.value}}, 'add', step, null);
    }
    case 'checkout': {
      const current = action.form === 'add' ? state.adding : state.fixing;
      if (current === null || current.saving !== null) return same(state);
      return fromForm(state, action.form, typed(current.path, action.value, state.lastToken), null);
    }
  }
}

/**
 * The fix for a missing checkout is inline: when the selected machine's checkout is not found and
 * no checkout form is open, its form opens with the path it had.
 */
function fixIfMissing(step: MachinesStep): MachinesStep {
  const machine = selectedMachine(step.state);
  if (machine === null || step.state.fixing !== null) return step;
  if (machineStatus(machine) !== 'checkout-not-found') return step;
  const opened = openFix(step.state, machine.host);
  return {state: opened.state, requests: [...step.requests, ...opened.requests]};
}

/** The next state and the requests to make, for `action` in `state`. */
export function machinesStep(state: MachinesState, action: MachinesAction): MachinesStep {
  switch (action.type) {
    case 'listed':
      if (state.listing?.token !== action.token) return same(state);
      if (!action.result.ok)
        return same({...state, listing: null, listProblem: action.result.error});
      return adoptList(state, action.result.value, state.listing.select);
    case 'reported': {
      const step = reported(state, action);
      return step.state === state ? step : fixIfMissing(step);
    }
    case 'select': {
      if (!state.machines.some(machine => machine.host === action.host)) return same(state);
      // The checkout form belongs to the machine shown; another machine's closes (and its check is
      // cancelled) when the selection moves.
      const closed =
        state.fixing !== null && state.fixing.host !== action.host
          ? close(state, 'fix')
          : same(state);
      const step = fixIfMissing(same({...closed.state, selected: action.host}));
      return {state: step.state, requests: [...closed.requests, ...step.requests]};
    }
    case 'open-add':
    case 'add-host':
    case 'checkout':
      return edit(state, action);
    case 'submit':
      return submit(state, action.form);
    case 'close':
      return close(state, action.form);
    case 'open-fix':
      return openFix(state, action.host);
    case 'completed':
    case 'inspected':
      return formReply(state, action);
    case 'saved':
      return saved(state, action);
    case 'remove':
    case 'sign-in':
      return startPending(state, action.type, action.host);
    case 'done':
      return done(state, action);
    case 'reconnect':
      return reconnect(state, action.host);
    case 'delivering':
      return delivering(state, action.hosts);
    case 'computed':
    case 'test-cluster':
    case 'tested':
      return fromPlaces(state, placesStep(placesOf(state), action));
  }
}

/** Every token the state awaits, by slot. */
export function machinesAwaited(state: MachinesState): Record<string, number | null> {
  const awaited: Record<string, number | null> = {
    listing: state.listing?.token ?? null,
    pending: state.pending?.token ?? null,
    'add:save': state.adding?.saving ?? null,
    'fix:save': state.fixing?.saving ?? null,
  };
  for (const machine of state.machines) awaited[`report:${machine.host}`] = machine.checking;
  Object.assign(awaited, placesAwaited(placesOf(state)));
  if (state.adding !== null) Object.assign(awaited, formAwaited(state.adding.path, 'add'));
  if (state.fixing !== null) Object.assign(awaited, formAwaited(state.fixing.path, 'fix'));
  return awaited;
}

/** What must hold of the machines a state names: each listed once, and each named one listed. */
function namingProblems(state: MachinesState): string[] {
  const problems: string[] = [];
  const hosts = state.machines.map(machine => machine.host);
  if (new Set(hosts).size !== hosts.length) problems.push('a machine is listed twice');
  const unselected = state.selected === null && hosts.length > 0 && state.listing === null;
  if (unselected || (state.selected !== null && !hosts.includes(state.selected))) {
    problems.push(`the selection ${state.selected} is not a listed machine`);
  }
  if (Object.keys(state.places).some(host => !hosts.includes(host)))
    problems.push('places are kept for a machine not listed');
  if (state.fixing !== null && !hosts.includes(state.fixing.host))
    problems.push('the checkout form is for a machine not listed');
  if (state.pending !== null && !hosts.includes(state.pending.host))
    problems.push('a pending action is for a machine not listed');
  if (state.pending?.kind === 'remove' && state.pending.host === hostKey({kind: 'local'}))
    problems.push('This Mac is being removed');
  return problems;
}

/** What must hold in every state; empty when all do. */
export function machinesInvariants(state: MachinesState): string[] {
  const problems = namingProblems(state);
  for (const form of [state.adding, state.fixing]) {
    if (form !== null && form.saving !== null && form.problem !== null)
      problems.push('a failed check shows while another is awaited');
  }
  for (const [slot, token] of Object.entries(machinesAwaited(state))) {
    if (token !== null && token > state.lastToken)
      problems.push(`${slot} awaits ${token}, past the last token`);
  }
  return problems;
}
