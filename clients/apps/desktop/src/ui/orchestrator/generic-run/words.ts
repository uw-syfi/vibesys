/**
 * What the generic run overview says. A strategy chooses its own verdicts, outcomes, roles and
 * metric names, so those are open text that is read as the words of its id; the closed sets the app
 * decodes itself (a lifecycle, a failure kind, a reason a section is short) are named here in plain
 * words, so a new kind cannot reach a screen as a protocol name.
 */
import {wordsOfId} from '@vibesys/display-names';
import type {RunDetailPresentation} from '../../../state/index.js';
import type {Word} from '../words.js';

type Presentation = RunDetailPresentation;
type Agent = Presentation['agents']['items'][number];
type Metric = Presentation['optimization']['metrics']['items'][number];
type Workstream = Presentation['workstreams']['items'][number];
type Gate = Presentation['correctness']['items'][number];
type AgentLifecycle = Agent['lifecycle'];
type MetricRole = Metric['role'];
type WorkstreamFailureKind = NonNullable<Workstream['workstreamFailure']>['kind'];
type GateStage = Gate['stage'];
type GateSubject = Gate['subject'];
type InputFailureKind = NonNullable<Gate['inputFailure']>['kind'];
type ProjectionAvailability = Presentation['agents']['availability'];
type ProjectionReason = Extract<
  ProjectionAvailability,
  {readonly kind: 'partial'}
>['reasons'][number];

const LIFECYCLE: Readonly<Record<AgentLifecycle, Word>> = {
  running: {label: 'Working', tone: 'live'},
  waiting: {label: 'Waiting', tone: 'neutral'},
  pending: {label: 'Queued', tone: 'neutral'},
  done: {label: 'Finished', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
  stopped: {label: 'Stopped', tone: 'warn'},
};

export const lifecycleWord = (lifecycle: AgentLifecycle): Word => LIFECYCLE[lifecycle];

/** Whether the span has ended, one way or another: nothing more will be recorded on it. */
export const isEnded = (lifecycle: AgentLifecycle): boolean =>
  lifecycle === 'done' || lifecycle === 'failed' || lifecycle === 'stopped';

const METRIC_ROLE: Readonly<Record<MetricRole, string>> = {
  objective: 'Objective',
  resource: 'Resource use',
  reference: 'Reference',
};

export const metricRoleWord = (role: MetricRole): string => METRIC_ROLE[role];

/** Words a person reads as capitals (`gpu_utilization` is "GPU utilization", `p99_latency` "P99 latency"). */
const ACRONYM = /\b(?:gpu|cpu|tpu|ram|vram|kv|io|api|llm|ttft|tpot|itl|rps|qps|p\d{2,3})\b/gi;

/** Open text a strategy or the run chose (a metric, a stage, a verdict), as words a person reads. */
export const openWords = (text: string): string =>
  wordsOfId(text).replace(ACRONYM, word => word.toUpperCase());

const STAGE: Readonly<Record<GateStage, string>> = {
  build: 'Build',
  accuracy: 'Accuracy',
  benchmark: 'Benchmark',
  validation: 'Validation',
  measurement: 'Measurement',
};

const SUBJECT: Readonly<Record<GateSubject, string>> = {
  input: 'The input',
  candidate: 'A candidate',
  unattributed: 'Not attributed',
};

export const stageWord = (stage: GateStage): string => STAGE[stage];
export const subjectWord = (subject: GateSubject): string => SUBJECT[subject];

const INPUT_FAILURE: Readonly<Record<InputFailureKind, string>> = {
  'no-evidence': 'No evidence came back',
  'readings-refused': 'Readings were refused',
  'readings-undecodable': 'Readings could not be read',
  'accuracy-failed': 'The accuracy check failed',
  'benchmark-failed': 'The benchmark failed',
  'wait-ended': 'The wait ended first',
};

export const inputFailureWord = (kind: InputFailureKind): string => INPUT_FAILURE[kind];

const WORKSTREAM_FAILURE: Readonly<Record<WorkstreamFailureKind, string>> = {
  stopped: 'Stopped before it finished',
  'parent-unverified': 'Its starting point could not be verified',
  refused: 'A step was refused',
  ended: 'The run ended first',
  'out-of-budget': 'Ran out of budget',
  'turn-failed': 'An agent turn failed',
  'invalid-reply': 'An agent gave an unusable reply',
  'render-failed': 'Its instructions could not be prepared',
  'wait-refused': 'Waiting for a result was refused',
  'checkpoint-declined': 'A checkpoint was declined',
  'no-changed-candidate': 'Produced no change to measure',
  'implementation-failed': 'The implementation failed',
  'review-rejected': 'The review rejected it',
  'no-trusted-evidence': 'No trusted measurement came back',
  'evidence-refused': 'The measurement was refused',
  'evidence-undecodable': 'The measurement could not be read',
  'profile-capture-unavailable': 'No profile could be captured',
  'turn-limit': 'Reached its turn limit',
  'repeated-turn': 'Repeated the same turn',
};

export const workstreamFailureWord = (kind: WorkstreamFailureKind): string =>
  WORKSTREAM_FAILURE[kind];

const REASON: Readonly<Record<ProjectionReason, string>> = {
  'declaration-unknown': 'The run did not say what this is.',
  'descriptor-unknown': 'The run uses a record format this app does not know.',
  'invalid-typed-fact': 'Some records could not be read.',
  'projection-not-installed': 'This app cannot show this kind of record yet.',
  'state-truncated': 'Older entries were left out to stay within the app’s limits.',
  'typed-fact-not-recorded': 'The run has not recorded any.',
};

/** Why a section is short or missing, or null when the host served it whole. */
export function availabilityNote(availability: ProjectionAvailability): string | null {
  switch (availability.kind) {
    case 'available':
      return null;
    case 'partial':
      return availability.reasons.map(reason => REASON[reason]).join(' ');
    case 'unavailable':
      return REASON[availability.reason];
  }
}
