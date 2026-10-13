import type {RunChannelRole} from '@vibesys/transport';

/** Every helper channel the host link owns, including its long-lived control helper. */
export type ChannelRole = 'host-control' | RunChannelRole;

export const CHANNEL_ROLES = ['host-control', 'run-control', 'subscribe', 'chat'] as const;

type LeasePhase = 'opening' | 'active';

interface ChannelLeaseState {
  readonly id: number;
  readonly role: ChannelRole;
  readonly phase: LeasePhase;
}

interface ChannelDemandState {
  readonly id: number;
  readonly role: ChannelRole;
  readonly order: number;
  readonly excludedMasters: readonly number[];
}

interface MasterAdmissionState {
  readonly id: number;
  /**
   * `retained` is a reachable spill whose last retirement failed: it may be reused, but is not
   * retried while idle. `unresolved` retains ownership when the shell cannot prove either presence
   * or absence; it consumes the pool bound but cannot receive a channel.
   */
  readonly phase: 'opening' | 'ready' | 'retiring' | 'retained' | 'unresolved';
  readonly leases: readonly ChannelLeaseState[];
}

/** What a bounded exit-and-reconcile operation proved about one retiring master. */
export type MasterRetirementOutcome = 'retired' | 'reachable' | 'unknown';

export interface AdmissionConfig {
  /** Maximum simultaneous helper sessions on one master. */
  readonly capacityPerMaster: number;
  /** Slots passive roles may not consume, kept for host and run control. */
  readonly reservedControlSlots: number;
  /** Bound on masters this app creates for one host. */
  readonly maxMasters: number;
}

export interface AdmissionState {
  readonly phase: 'accepting' | 'closed';
  readonly config: AdmissionConfig;
  readonly nextMasterId: number;
  readonly nextDemandOrder: number;
  readonly masters: readonly MasterAdmissionState[];
  readonly demands: readonly ChannelDemandState[];
}

export type AdmissionEvent =
  | {
      readonly type: 'request';
      readonly demandId: number;
      readonly role: ChannelRole;
      readonly excludedMasters?: readonly number[];
    }
  | {readonly type: 'cancel'; readonly demandId: number}
  | {readonly type: 'master-ready'; readonly masterId: number}
  | {readonly type: 'master-failed'; readonly masterId: number}
  | {readonly type: 'activate'; readonly leaseId: number}
  | {readonly type: 'release'; readonly leaseId: number}
  | {
      readonly type: 'master-retirement-observed';
      readonly masterId: number;
      readonly outcome: MasterRetirementOutcome;
    }
  | {readonly type: 'close'};

export type AdmissionOutput =
  | {readonly type: 'granted'; readonly demandId: number; readonly masterId: number}
  | {readonly type: 'capacity'; readonly demandId: number}
  | {readonly type: 'cancelled'; readonly demandId: number}
  | {readonly type: 'closed'; readonly demandId: number}
  | {readonly type: 'start-master'; readonly masterId: number}
  | {readonly type: 'stop-master'; readonly masterId: number};

export interface AdmissionTransition {
  readonly state: AdmissionState;
  readonly outputs: readonly AdmissionOutput[];
}

export interface ChannelCounts {
  readonly 'host-control': number;
  readonly 'run-control': number;
  readonly subscribe: number;
  readonly chat: number;
}

export interface MasterPoolDiagnostic {
  readonly opening: ChannelCounts;
  readonly active: ChannelCounts;
}

export interface LinkPoolDiagnostics {
  readonly masterCount: number;
  readonly opening: ChannelCounts;
  readonly active: ChannelCounts;
  readonly masters: readonly MasterPoolDiagnostic[];
}

const EMPTY_COUNTS: ChannelCounts = {
  'host-control': 0,
  'run-control': 0,
  subscribe: 0,
  chat: 0,
};

function validConfig(config: AdmissionConfig): void {
  if (!Number.isSafeInteger(config.capacityPerMaster) || config.capacityPerMaster < 1) {
    throw new RangeError('capacityPerMaster must be a positive integer');
  }
  if (
    !Number.isSafeInteger(config.reservedControlSlots) ||
    config.reservedControlSlots < 0 ||
    config.reservedControlSlots >= config.capacityPerMaster
  ) {
    throw new RangeError('reservedControlSlots must be below capacityPerMaster');
  }
  if (!Number.isSafeInteger(config.maxMasters) || config.maxMasters < 1) {
    throw new RangeError('maxMasters must be a positive integer');
  }
}

/** Start with the base master opening; the shell reports when its process answers. */
export function initialAdmissionState(config: AdmissionConfig): AdmissionState {
  validConfig(config);
  return {
    phase: 'accepting',
    config,
    nextMasterId: 1,
    nextDemandOrder: 0,
    masters: [{id: 0, phase: 'opening', leases: []}],
    demands: [],
  };
}

function isOperator(role: ChannelRole): boolean {
  return rolePriority(role) <= rolePriority('run-control');
}

/** Stable priority for callers that enqueue more than one role at one boundary. */
export function rolePriority(role: ChannelRole): number {
  switch (role) {
    case 'host-control':
      return 0;
    case 'run-control':
      return 1;
    case 'subscribe':
      return 2;
    case 'chat':
      return 3;
    default:
      throw new TypeError(`unknown channel role: ${String(role)}`);
  }
}

function admits(master: MasterAdmissionState, config: AdmissionConfig, role: ChannelRole): boolean {
  if (master.phase !== 'ready' && master.phase !== 'retained') return false;
  const limit = isOperator(role)
    ? config.capacityPerMaster
    : config.capacityPerMaster - config.reservedControlSlots;
  return master.leases.length < limit;
}

function replaceMaster(
  state: AdmissionState,
  id: number,
  update: (master: MasterAdmissionState) => MasterAdmissionState | null,
): AdmissionState {
  const masters: MasterAdmissionState[] = [];
  let found = false;
  for (const master of state.masters) {
    if (master.id !== id) {
      masters.push(master);
      continue;
    }
    found = true;
    const changed = update(master);
    if (changed !== null) masters.push(changed);
  }
  if (!found) throw new Error(`unknown SSH master ${id}`);
  return {...state, masters};
}

function orderedDemands(demands: readonly ChannelDemandState[]): readonly ChannelDemandState[] {
  return [...demands].sort(
    (left, right) => rolePriority(left.role) - rolePriority(right.role) || left.order - right.order,
  );
}

function eligibleMaster(
  state: AdmissionState,
  demand: ChannelDemandState,
): MasterAdmissionState | undefined {
  return state.masters
    .filter(
      master =>
        !demand.excludedMasters.includes(master.id) && admits(master, state.config, demand.role),
    )
    .sort((left, right) => left.leases.length - right.leases.length || left.id - right.id)[0];
}

function grant(
  state: AdmissionState,
  demand: ChannelDemandState,
  master: MasterAdmissionState,
): AdmissionState {
  return replaceMaster(
    {...state, demands: state.demands.filter(candidate => candidate.id !== demand.id)},
    master.id,
    current => ({
      ...current,
      phase: 'ready',
      leases: [...current.leases, {id: demand.id, role: demand.role, phase: 'opening'}],
    }),
  );
}

function canWaitForOwnedMaster(state: AdmissionState, demand: ChannelDemandState): boolean {
  return state.masters.some(
    master =>
      master.phase === 'retiring' ||
      (master.phase === 'opening' && !demand.excludedMasters.includes(master.id)),
  );
}

/**
 * Closing never creates masters. It retires each reachable idle master once; an unresolved result
 * stays owned so the shell can report incomplete teardown instead of spinning or dropping it.
 */
function scheduleClosing(initial: AdmissionState): AdmissionTransition {
  const idle = initial.masters.filter(
    master =>
      (master.phase === 'ready' || master.phase === 'retained') && master.leases.length === 0,
  );
  if (idle.length === 0) return {state: initial, outputs: []};
  const ids = new Set(idle.map(master => master.id));
  return {
    state: {
      ...initial,
      masters: initial.masters.map(master =>
        ids.has(master.id) ? {...master, phase: 'retiring'} : master,
      ),
    },
    outputs: idle.map(master => ({type: 'stop-master', masterId: master.id})),
  };
}

/**
 * Drain queued demand in stable role-priority/FIFO order. A ready spill master is offered to all
 * queued callers before it can be retired, so cancellation of its original requester cannot strand
 * a follower. At most one opening master is needed: when it resolves this reducer runs again.
 */
function schedule(initial: AdmissionState): AdmissionTransition {
  let state = initial;
  const outputs: AdmissionOutput[] = [];
  if (state.phase === 'closed') return scheduleClosing(state);

  while (state.demands.length > 0) {
    const demand = orderedDemands(state.demands)[0];
    if (demand === undefined) break;
    const master = eligibleMaster(state, demand);
    if (master !== undefined) {
      state = grant(state, demand, master);
      outputs.push({type: 'granted', demandId: demand.id, masterId: master.id});
      continue;
    }
    if (canWaitForOwnedMaster(state, demand)) break;
    if (state.masters.length < state.config.maxMasters) {
      const masterId = state.nextMasterId;
      state = {
        ...state,
        nextMasterId: masterId + 1,
        masters: [...state.masters, {id: masterId, phase: 'opening', leases: []}],
      };
      outputs.push({type: 'start-master', masterId});
      break;
    }
    state = {...state, demands: state.demands.filter(candidate => candidate.id !== demand.id)};
    outputs.push({type: 'capacity', demandId: demand.id});
  }

  if (state.demands.length === 0) {
    const idleSpills = state.masters.filter(
      master => master.id !== 0 && master.phase === 'ready' && master.leases.length === 0,
    );
    if (idleSpills.length > 0) {
      const ids = new Set(idleSpills.map(master => master.id));
      state = {
        ...state,
        masters: state.masters.map(master =>
          ids.has(master.id) ? {...master, phase: 'retiring'} : master,
        ),
      };
      outputs.push(
        ...idleSpills.map(master => ({type: 'stop-master' as const, masterId: master.id})),
      );
    }
  }
  return {state, outputs};
}

function requestChannel(
  state: AdmissionState,
  event: Extract<AdmissionEvent, {readonly type: 'request'}>,
): AdmissionTransition {
  rolePriority(event.role);
  const duplicate =
    state.demands.some(demand => demand.id === event.demandId) ||
    state.masters.some(master => master.leases.some(lease => lease.id === event.demandId));
  if (duplicate) throw new Error(`duplicate channel demand ${event.demandId}`);
  if (state.phase === 'closed') {
    return {state, outputs: [{type: 'closed', demandId: event.demandId}]};
  }
  const demand: ChannelDemandState = {
    id: event.demandId,
    role: event.role,
    order: state.nextDemandOrder,
    excludedMasters: [...new Set(event.excludedMasters ?? [])].sort((left, right) => left - right),
  };
  return schedule({
    ...state,
    nextDemandOrder: state.nextDemandOrder + 1,
    demands: [...state.demands, demand],
  });
}

function cancelChannel(state: AdmissionState, demandId: number): AdmissionTransition {
  if (!state.demands.some(demand => demand.id === demandId)) return {state, outputs: []};
  const transition = schedule({
    ...state,
    demands: state.demands.filter(demand => demand.id !== demandId),
  });
  return {state: transition.state, outputs: [{type: 'cancelled', demandId}, ...transition.outputs]};
}

function updateLease(
  state: AdmissionState,
  leaseId: number,
  update: (lease: ChannelLeaseState) => ChannelLeaseState | null,
): AdmissionState {
  let found = false;
  const masters = state.masters.map(master => ({
    ...master,
    leases: master.leases.flatMap(lease => {
      if (lease.id !== leaseId) return [lease];
      found = true;
      const changed = update(lease);
      return changed === null ? [] : [changed];
    }),
  }));
  if (!found) throw new Error(`unknown channel lease ${leaseId}`);
  return {...state, masters};
}

function readyMaster(state: AdmissionState, masterId: number): AdmissionTransition {
  return schedule(
    replaceMaster(state, masterId, master => {
      if (master.phase !== 'opening') throw new Error(`SSH master ${masterId} is not opening`);
      return {...master, phase: 'ready'};
    }),
  );
}

function failMaster(state: AdmissionState, masterId: number): AdmissionTransition {
  const retiring = replaceMaster(state, masterId, master => {
    if (master.phase !== 'opening') throw new Error(`SSH master ${masterId} is not opening`);
    if (master.leases.length !== 0) {
      throw new Error(`failed SSH master ${masterId} owns channel leases`);
    }
    return {...master, phase: 'retiring'};
  });
  // The creation command can fail after ControlPersist daemonized the master. Keep ownership until
  // the bounded retirement path proves absence, while reporting only auxiliary-capacity failure to
  // callers so a spill cannot misclassify the established base master as unhealthy.
  const outputs: AdmissionOutput[] = orderedDemands(retiring.demands).map(demand => ({
    type: 'capacity',
    demandId: demand.id,
  }));
  outputs.push({type: 'stop-master', masterId});
  return {state: {...retiring, demands: []}, outputs};
}

function activateChannel(state: AdmissionState, leaseId: number): AdmissionTransition {
  return {
    state: updateLease(state, leaseId, lease => {
      if (lease.phase !== 'opening') throw new Error(`channel lease ${leaseId} is already active`);
      return {...lease, phase: 'active'};
    }),
    outputs: [],
  };
}

function releaseChannel(state: AdmissionState, leaseId: number): AdmissionTransition {
  return schedule(updateLease(state, leaseId, () => null));
}

function observeRetirement(
  state: AdmissionState,
  masterId: number,
  outcome: MasterRetirementOutcome,
): AdmissionTransition {
  const observed = replaceMaster(state, masterId, master => {
    if (master.phase !== 'retiring') throw new Error(`SSH master ${masterId} is not retiring`);
    if (master.leases.length !== 0) throw new Error(`SSH master ${masterId} still owns channels`);
    if (outcome === 'retired') return null;
    if (state.phase === 'closed' || outcome === 'unknown') {
      return {...master, phase: 'unresolved'};
    }
    return {...master, phase: 'retained'};
  });
  // A failed close attempt is terminal for this release. It remains accounted for so the shell can
  // reject with truthful diagnostics rather than retrying forever or claiming it disappeared.
  if (state.phase === 'closed' && outcome !== 'retired') return {state: observed, outputs: []};
  return schedule(observed);
}

function closeAdmission(state: AdmissionState): AdmissionTransition {
  if (state.phase === 'closed') return {state, outputs: []};
  const terminal: AdmissionOutput[] = orderedDemands(state.demands).map(demand => ({
    type: 'closed',
    demandId: demand.id,
  }));
  // `unresolved` masters get one explicit final reconciliation during close. A later failed result
  // remains unresolved and is not automatically retried.
  const closed: AdmissionState = {
    ...state,
    phase: 'closed',
    demands: [],
    masters: state.masters.map(master =>
      master.phase === 'unresolved' ? {...master, phase: 'retained'} : master,
    ),
  };
  const retirement = scheduleClosing(closed);
  return {state: retirement.state, outputs: [...terminal, ...retirement.outputs]};
}

/** Pure transition boundary for all queued demand, lease, and master lifecycle accounting. */
export function stepAdmission(state: AdmissionState, event: AdmissionEvent): AdmissionTransition {
  switch (event.type) {
    case 'request':
      return requestChannel(state, event);
    case 'cancel':
      return cancelChannel(state, event.demandId);
    case 'master-ready':
      return readyMaster(state, event.masterId);
    case 'master-failed':
      return failMaster(state, event.masterId);
    case 'activate':
      return activateChannel(state, event.leaseId);
    case 'release':
      return releaseChannel(state, event.leaseId);
    case 'master-retirement-observed':
      return observeRetirement(state, event.masterId, event.outcome);
    case 'close':
      return closeAdmission(state);
  }
}

function counts(leases: readonly ChannelLeaseState[], phase: LeasePhase): ChannelCounts {
  const result = {...EMPTY_COUNTS};
  for (const lease of leases) {
    if (lease.phase === phase) result[lease.role] += 1;
  }
  return result;
}

function addCounts(left: ChannelCounts, right: ChannelCounts): ChannelCounts {
  return {
    'host-control': left['host-control'] + right['host-control'],
    'run-control': left['run-control'] + right['run-control'],
    subscribe: left.subscribe + right.subscribe,
    chat: left.chat + right.chat,
  };
}

/** A bounded projection: counts only, with no host, command, path, or failure detail. */
export function poolDiagnostics(state: AdmissionState): LinkPoolDiagnostics {
  let opening = {...EMPTY_COUNTS};
  let active = {...EMPTY_COUNTS};
  const masters = [...state.masters]
    .sort((left, right) => left.id - right.id)
    .map(master => {
      const masterOpening = counts(master.leases, 'opening');
      const masterActive = counts(master.leases, 'active');
      opening = addCounts(opening, masterOpening);
      active = addCounts(active, masterActive);
      return {opening: masterOpening, active: masterActive};
    });
  return {masterCount: masters.length, opening, active, masters};
}
