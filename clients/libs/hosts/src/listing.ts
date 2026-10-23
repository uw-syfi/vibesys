/**
 * A host's runs, as its registry lists them, and the pure projection of what a host's helper
 * reports into that model.
 */
import type {
  InstanceList,
  LiveInstanceRecord,
  RunPhase,
  RunStatus,
  UnreadableRecord,
} from '@vibesys/contracts/host-helper';
import type {ConnectionStatus} from './connection-status.js';
import type {HostKey} from './host.js';

/** Who paused a paused run: the user, or a usage limit. */
export type RunPause = NonNullable<LiveInstanceRecord['paused']> | null;
/** A tombstone's end record: the outcome, a one-line reason, and whether it can resume. */
export type RunEnd = NonNullable<LiveInstanceRecord['end']> | null;

/** One run a host's registry lists: a live server, or an ended one kept as a tombstone. */
export interface HostRun {
  /** The registry id of the server. */
  readonly id: string;
  readonly projectRoot: string;
  readonly runId: string;
  /** The run's phase (`RunStatus`) in words, with its startup stage while starting. */
  readonly status: string;
  /** The run's phase as the registry records it. */
  readonly phase: RunStatus;
  /** Who paused it, exactly while its phase is `paused`. */
  readonly paused: RunPause;
  /** How it ended, on a tombstone that records it; null while live, or on an older tombstone. */
  readonly end: RunEnd;
  readonly startedAt: number;
  /** Why the run failed, when it did. */
  readonly reason: string | null;
}

/** A host's registry as the views show it, or why it cannot be read now. */
export type HostListing =
  | {
      readonly kind: 'listed';
      readonly host: HostKey;
      readonly live: readonly HostRun[];
      readonly ended: readonly HostRun[];
      /** Records this app cannot read (another VibeSys version), by id. */
      readonly unreadable: readonly string[];
      /** Runs whose project cannot be resolved; liveness may also be unknown. */
      readonly unknownProject?: readonly (Omit<HostRun, 'projectRoot'> & {
        readonly alive?: boolean;
      })[];
      /** Ids whose lock could not be read: neither known live nor known ended, by id. */
      readonly unverified: readonly string[];
    }
  | {
      readonly kind: 'unavailable';
      readonly host: HostKey;
      readonly connection: ConnectionStatus;
    };

type ListedHost = Extract<HostListing, {readonly kind: 'listed'}>;

/**
 * The hosts that answer in `after` and did not in `before` (absent counts as not answering): a
 * read that failed because such a host was unreachable may now succeed.
 */
export function hostsComeUp(
  before: readonly HostListing[],
  after: readonly HostListing[],
): readonly HostKey[] {
  const was = new Set(before.flatMap(each => (each.kind === 'listed' ? [each.host] : [])));
  return after.flatMap(each => (each.kind === 'listed' && !was.has(each.host) ? [each.host] : []));
}

/** What one coherent registry listing proves about an instance. */
export type InstanceObservation<Record, Unreadable = string> =
  | {readonly kind: 'live'; readonly record: Record}
  | {readonly kind: 'ended'; readonly record: Record}
  /** Its lock could not be read, so liveness is unknown. */
  | {readonly kind: 'unverified'}
  /** Its record cannot be interpreted by this app, so liveness is unknown. */
  | {readonly kind: 'unreadable'; readonly record: Unreadable}
  /** The listing contains no evidence for this id. */
  | {readonly kind: 'absent'};

const STAGES: Readonly<Record<NonNullable<RunPhase['stage']>, string>> = {
  resolving: 'Resolving',
  probing_workspace: 'Probing workspace',
  building_image: 'Building image',
  building_tools: 'Building tools',
  starting_container: 'Starting container',
};

/**
 * A run's phase in words: a starting run's stage and progress (`Building image 12/30: RUN ...`, so a
 * long first image build is visible), otherwise its status.
 */
export function instancePhaseText(phase: RunPhase): string {
  if (phase.status !== 'starting' || phase.stage == null) return phase.status;
  const stage = STAGES[phase.stage];
  const progress = phase.progress;
  if (progress == null) return stage;
  const count = `${progress.done}/${progress.total ?? '?'}`;
  return progress.label == null || progress.label === ''
    ? `${stage} ${count}`
    : `${stage} ${count}: ${progress.label}`;
}

function hostRun(record: LiveInstanceRecord): HostRun {
  const {phase} = record;
  return {
    id: record.id,
    projectRoot: record.project_root,
    runId: record.run_id,
    status: instancePhaseText(phase),
    phase: phase.status,
    paused: record.paused ?? null,
    end: record.end ?? null,
    startedAt: record.started_at,
    reason: phase.reason == null ? null : phase.reason.message,
  };
}

/**
 * Whether the directory the app remembers a run in names the registry's `root` for it. The app keeps
 * what the user typed (`~/x`, a trailing slash) and the registry the resolved absolute path.
 */
function sameProject(remembered: string, root: string): boolean {
  const trimmed = (path: string): string => path.replace(/\/+$/, '');
  const [want, have] = [trimmed(remembered), trimmed(root)];
  return want === have || (want.startsWith('~/') && have.endsWith(want.slice(1)));
}

/**
 * The newest of `servers` (a host's live or ended records) that drives or drove run `runId` in
 * `project`: a run resumed many times has one record per server, and the latest speaks for it.
 */
export function newestServerOf(
  servers: readonly HostRun[],
  runId: string,
  project?: string,
): HostRun | undefined {
  return servers
    .filter(
      server =>
        server.runId === runId &&
        (project === undefined || sameProject(project, server.projectRoot)),
    )
    .reduce<HostRun | undefined>(
      (best, server) => (best === undefined || best.startedAt < server.startedAt ? server : best),
      undefined,
    );
}

/** `listing` from `host`'s registry, as the views list it. */
export function hostListing(host: HostKey, listing: InstanceList): HostListing {
  return {
    kind: 'listed',
    host,
    live: listing.instances.map(hostRun),
    ended: (listing.ended ?? []).map(hostRun),
    unreadable: (listing.unreadable ?? []).map(entry => entry.id).sort(),
    unverified: [...(listing.unverified ?? [])].sort(),
  };
}

/**
 * The registry's observation for `instanceId`, shared by transport and view projections.
 *
 * A valid record is stronger evidence than an overlapping uncertainty bucket (real registry
 * listings partition ids, but keeping precedence here makes every consumer agree even on a
 * synthesized listing). Without one, unverified wins over unreadable because a failed lock read
 * cannot establish what record owns the id. Absence alone is the only remaining verdict.
 */
export function instanceObservation(
  listing: InstanceList,
  instanceId: string,
): InstanceObservation<LiveInstanceRecord, UnreadableRecord>;
export function instanceObservation(
  listing: ListedHost,
  instanceId: string,
): InstanceObservation<HostRun>;
export function instanceObservation(
  listing: InstanceList | ListedHost,
  instanceId: string,
): InstanceObservation<LiveInstanceRecord | HostRun, UnreadableRecord | string> {
  const live = ('instances' in listing ? listing.instances : listing.live).find(
    record => record.id === instanceId,
  );
  if (live !== undefined) return {kind: 'live', record: live};

  const ended = (listing.ended ?? []).find(record => record.id === instanceId);
  if (ended !== undefined) return {kind: 'ended', record: ended};
  if ((listing.unverified ?? []).includes(instanceId)) return {kind: 'unverified'};

  const unreadable = (listing.unreadable ?? []).find(record =>
    typeof record === 'string' ? record === instanceId : record.id === instanceId,
  );
  if (unreadable !== undefined) return {kind: 'unreadable', record: unreadable};
  return {kind: 'absent'};
}
