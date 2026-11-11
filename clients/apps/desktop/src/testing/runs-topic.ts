/** Valid recordings of the host's `runs` Subscribe topic. */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  HostService,
  RunEntrySchema,
  RunLiveness,
  SubscribeResponseSchema,
  type TopicFailureCode,
} from '@vibesys/proto';
import type {Recording, Step} from './transport/index.js';

export const RUNS_SUBSCRIBE = `/${HostService.typeName}/${HostService.method.subscribe.name}`;

type RunInit = MessageInitShape<typeof RunEntrySchema>;
type FrameInit = NonNullable<MessageInitShape<typeof SubscribeResponseSchema>['frame']>;

const frame = (init: FrameInit): Step => ({
  frame: toBinary(SubscribeResponseSchema, create(SubscribeResponseSchema, {frame: init})),
});

/** The keyed run now has `entry`. */
export const runEntryFrame = (key: string, entry: RunInit = {}): Step =>
  frame({
    case: 'entry',
    value: {
      key,
      value: {
        case: 'runs',
        value: create(RunEntrySchema, {liveness: RunLiveness.UNSPECIFIED, ...entry}),
      },
    },
  });

export const runRemovedFrame = (key: string): Step => frame({case: 'removed', value: key});
export const runsSyncedFrame: Step = frame({case: 'synced', value: {}});
export const runsHeartbeatFrame: Step = frame({case: 'heartbeat', value: {}});
export const runsRefusedFrame = (code: TopicFailureCode): Step =>
  frame({case: 'failed', value: {code, detail: 'refused'}});

/** `base`, with each Subscribe call answered by `streams`. */
export const servingRuns =
  (streams: (nth: number) => ReadonlyArray<Step>) =>
  (base: Recording): Recording => ({
    respond: (target, nth) =>
      target.method === RUNS_SUBSCRIBE ? streams(nth) : base.respond(target, nth),
  });
