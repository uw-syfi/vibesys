/**
 * The closed list of topics a client can subscribe to. A topic is a descriptor over the generated
 * request and policy: which entries are its own, and how its keyed map becomes the value a screen
 * reads. Adding an arm to `SubscribeRequest.topic` fails to compile here until its descriptor is
 * registered, and the contract suite then runs every registered topic.
 */
import {create} from '@bufbuild/protobuf';
import {type RecordedProjectRoot, recordedProjectRoot} from '@vibesys/hosts';
import {
  HostService,
  type MachineStats,
  MachineStatsTopicSchema,
  type RunHead,
  RunLiveness,
  RunsTopicSchema,
  type SubscribeRequest,
  SubscribeRequestSchema,
  type TopicEntry,
  topicPolicies,
} from '@vibesys/proto';

/** The gateway's RPC for every topic. Only this library and main's hub call it. */
export const SUBSCRIBE_METHOD = `/${HostService.typeName}/${HostService.method.subscribe.name}`;

/** A topic's bounds, which the gateway and every client read from one descriptor. */
export interface TopicPolicy {
  readonly cadenceMs: number;
  readonly staleAfterMs: number;
  readonly firstFrameMs: number;
  readonly minIntervalMs: number;
  readonly maxKeys: number;
  readonly maxValueBytes: number;
  /** What the gateway holds a source to; no client waits on it. */
  readonly sourceLeaseMs: number;
}

/** The `SubscribeRequest.topic` case of each topic, which is also its `TopicEntry.value` case. */
export type TopicArm = NonNullable<SubscribeRequest['topic']['case']>;

export interface Topic<S> {
  readonly arm: TopicArm;
  readonly request: SubscribeRequest;
  readonly policy: TopicPolicy;
  /** Whether an entry is one of this topic's values. Any other entry breaks the protocol. */
  accepts(entry: TopicEntry): boolean;
  /** The value of a complete snapshot, or null when the snapshot is not a valid one of this topic. */
  fold(entries: ReadonlyMap<string, TopicEntry>): S | null;
}

/** The value of the `machineStats` topic: CPU, memory and per-GPU figures of one host. */
export type {MachineStats};

/** A topic of any value type, as the registry and main's hub hold it. */
export type AnyTopic = Topic<unknown>;

/** The one value of a topic with one key, whatever the key is. */
function onlyValue<S>(
  entries: ReadonlyMap<string, TopicEntry>,
  of: (entry: TopicEntry) => S | null,
): S | null {
  const [only, ...rest] = entries.values();
  return only === undefined || rest.length > 0 ? null : of(only);
}

/** CPU, memory and per-GPU figures of one host. */
export const machineStats: Topic<MachineStats> = {
  arm: 'machineStats',
  request: create(SubscribeRequestSchema, {
    topic: {case: 'machineStats', value: create(MachineStatsTopicSchema)},
  }),
  policy: topicPolicies.machineStats,
  accepts: entry => entry.value.case === 'machineStats',
  fold: entries =>
    onlyValue(entries, entry => (entry.value.case === 'machineStats' ? entry.value.value : null)),
};

/** What a host last said of one run: its head (absent for a phase 1 run), liveness and project. */
export interface RunFacts {
  readonly projectRoot: RecordedProjectRoot | undefined;
  readonly head: RunHead | undefined;
  readonly liveness: RunLiveness;
}

/** The runs of one host's run index, by run id. */
export type HostRunFacts = ReadonlyMap<string, RunFacts>;

/** How many of a host's runs have a writer alive: the one meaning of "live" for a run count. */
export function aliveRuns(runs: HostRunFacts): number {
  let alive = 0;
  for (const run of runs.values()) if (run.liveness === RunLiveness.ALIVE) alive += 1;
  return alive;
}

/** The run list of one host, and with a key, one run's facts. */
export const runs: Topic<HostRunFacts> = {
  arm: 'runs',
  request: create(SubscribeRequestSchema, {topic: {case: 'runs', value: create(RunsTopicSchema)}}),
  policy: topicPolicies.runs,
  accepts: entry => entry.value.case === 'runs',
  fold: entries => {
    const facts = new Map<string, RunFacts>();
    for (const [runId, entry] of entries) {
      if (entry.value.case !== 'runs') return null;
      const {head, liveness, projectRoot} = entry.value.value;
      facts.set(runId, {projectRoot: recordedProjectRoot(projectRoot), head, liveness});
    }
    return facts;
  },
};

/** Every topic by its arm; an arm of the request with no entry here fails to compile. */
export const TOPICS = {machineStats, runs} satisfies Record<TopicArm, AnyTopic>;

/** Every topic, for the contract suite and for tools that list them. */
export const ALL_TOPICS: readonly AnyTopic[] = Object.values(TOPICS);

/** The topic a request names, or null for a request this client does not know. */
export function topicOf(request: SubscribeRequest): AnyTopic | null {
  const arm = request.topic.case;
  return arm === undefined ? null : TOPICS[arm];
}
