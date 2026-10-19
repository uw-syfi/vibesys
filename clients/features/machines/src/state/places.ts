/**
 * The places reached from a machine: the Slurm clusters and the Modal cloud its own settings name
 * (`compute`), and what the last test of each cluster found. A pure fold over replies, whose
 * tokens come from the Machines store's one counter, so a late reply (a machine read again since,
 * a cluster removed or tested again since) is dropped. A machine's rows are exactly the places
 * its own settings list: nothing is shared between machines.
 */
import type {ClusterTest, ComputeSettings, Offered, SlurmClusterSettings} from '@vibesys/ipc';

/** A machine's key, as the Machines screen lists it. */
type HostKey = string;

type Hardware = NonNullable<SlurmClusterSettings['hardware']>;

type Outcome<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string};

/** The machine's settings: awaited, answered (its helper may predate them), or failed. */
export type PlacesReading =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'ready'; readonly offered: Offered<ComputeSettings>}
  | {readonly kind: 'failed'; readonly error: string};

/** What the last test of a cluster found, and when (milliseconds, by the clock the shell gave). */
export type LastCheck =
  | {readonly kind: 'passed'; readonly at: number; readonly hardware: Hardware}
  | {
      readonly kind: 'failed';
      readonly at: number;
      /** When the cluster was first found unreachable, in this run of failed tests. */
      readonly since: number;
      readonly detail: string;
    };

export interface ClusterCheck {
  /** The test awaited, or null. */
  readonly testing: number | null;
  readonly last: LastCheck | null;
}

export interface Places {
  readonly reading: PlacesReading;
  /** By cluster name. */
  readonly checks: Readonly<Record<string, ClusterCheck>>;
}

export interface PlacesState {
  readonly places: Readonly<Record<HostKey, Places>>;
  readonly lastToken: number;
}

export type PlacesAction =
  | {
      readonly type: 'computed';
      readonly token: number;
      readonly host: HostKey;
      readonly result: Outcome<Offered<ComputeSettings>>;
    }
  | {readonly type: 'test-cluster'; readonly host: HostKey; readonly name: string}
  | {
      readonly type: 'tested';
      readonly token: number;
      readonly host: HostKey;
      readonly name: string;
      readonly at: number;
      readonly result: Outcome<ClusterTest>;
    };

export type PlacesRequest =
  | {readonly kind: 'compute'; readonly token: number; readonly host: HostKey}
  | {
      readonly kind: 'test-cluster';
      readonly token: number;
      readonly host: HostKey;
      readonly cluster: SlurmClusterSettings;
    };

export interface PlacesStep {
  readonly state: PlacesState;
  readonly requests: readonly PlacesRequest[];
}

const IDLE: ClusterCheck = {testing: null, last: null};

function withPlaces(
  state: PlacesState,
  host: HostKey,
  change: (places: Places) => Places,
): PlacesState {
  const current = state.places[host];
  if (current === undefined) return state;
  const next = change(current);
  return next === current ? state : {...state, places: {...state.places, [host]: next}};
}

/** The clusters `places` lists, in the order its settings give them; none until it is read. */
export function clustersOf(places: Places | undefined): readonly SlurmClusterSettings[] {
  if (places?.reading.kind !== 'ready' || places.reading.offered.kind !== 'offered') return [];
  return places.reading.offered.value.clusters.map(listed => listed.cluster);
}

/** Read `host`'s settings (again): its tests so far stay until the answer says what remains. */
export function readPlaces(state: PlacesState, host: HostKey): PlacesStep {
  const token = state.lastToken + 1;
  const checks = state.places[host]?.checks ?? {};
  const places: Places = {reading: {kind: 'loading', token}, checks};
  return {
    state: {places: {...state.places, [host]: places}, lastToken: token},
    requests: [{kind: 'compute', token, host}],
  };
}

/** `state` with only the places of `hosts`. */
export function keepPlaces(state: PlacesState, hosts: ReadonlySet<HostKey>): PlacesState {
  const kept = Object.entries(state.places).filter(([host]) => hosts.has(host));
  return {...state, places: Object.fromEntries(kept)};
}

function computed(
  state: PlacesState,
  action: Extract<PlacesAction, {type: 'computed'}>,
): PlacesState {
  return withPlaces(state, action.host, current => {
    if (current.reading.kind !== 'loading' || current.reading.token !== action.token) {
      return current;
    }
    if (!action.result.ok) {
      return {...current, reading: {kind: 'failed', error: action.result.error}};
    }
    const offered = action.result.value;
    const named = new Set(
      offered.kind === 'offered' ? offered.value.clusters.map(each => each.cluster.name) : [],
    );
    const checks = Object.fromEntries(
      Object.entries(current.checks).filter(([name]) => named.has(name)),
    );
    return {reading: {kind: 'ready', offered}, checks};
  });
}

function startTest(state: PlacesState, host: HostKey, name: string): PlacesStep {
  const places = state.places[host];
  const cluster = clustersOf(places).find(each => each.name === name);
  const check = places?.checks[name] ?? IDLE;
  if (places === undefined || cluster === undefined || check.testing !== null) {
    return {state, requests: []};
  }
  const token = state.lastToken + 1;
  const next = withPlaces(state, host, current => ({
    ...current,
    checks: {...current.checks, [name]: {...check, testing: token}},
  }));
  return {
    state: {...next, lastToken: token},
    requests: [{kind: 'test-cluster', token, host, cluster}],
  };
}

function lastOf(previous: LastCheck | null, at: number, result: Outcome<ClusterTest>): LastCheck {
  if (result.ok && result.value.outcome === 'passed') {
    return {kind: 'passed', at, hardware: result.value.hardware};
  }
  const detail = result.ok && result.value.outcome === 'failed' ? result.value.detail : '';
  const reason = result.ok ? detail : result.error;
  return {
    kind: 'failed',
    at,
    since: previous?.kind === 'failed' ? previous.since : at,
    detail: reason,
  };
}

function tested(state: PlacesState, action: Extract<PlacesAction, {type: 'tested'}>): PlacesState {
  return withPlaces(state, action.host, current => {
    const check = current.checks[action.name];
    if (check === undefined || check.testing !== action.token) return current;
    const last = lastOf(check.last, action.at, action.result);
    return {...current, checks: {...current.checks, [action.name]: {testing: null, last}}};
  });
}

/** The next state and the requests to make, for a places `action`. */
export function placesStep(state: PlacesState, action: PlacesAction): PlacesStep {
  switch (action.type) {
    case 'computed':
      return {state: computed(state, action), requests: []};
    case 'test-cluster':
      return startTest(state, action.host, action.name);
    case 'tested':
      return {state: tested(state, action), requests: []};
  }
}

/** Every token the places await, by slot. */
export function placesAwaited(state: PlacesState): Record<string, number | null> {
  const awaited: Record<string, number | null> = {};
  for (const [host, places] of Object.entries(state.places)) {
    awaited[`compute:${host}`] = places.reading.kind === 'loading' ? places.reading.token : null;
    for (const [name, check] of Object.entries(places.checks)) {
      awaited[`test:${host}:${name}`] = check.testing;
    }
  }
  return awaited;
}

/** The clusters whose last test found them unreachable, over every machine. */
export function unreachableClusters(state: PlacesState): number {
  return Object.values(state.places)
    .flatMap(places => Object.values(places.checks))
    .filter(check => check.last?.kind === 'failed').length;
}
