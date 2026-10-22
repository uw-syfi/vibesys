import type {EpochVersion, Keyframe, Lifecycle, MetricHistory, Span} from '@vibesys/proto';
/** Content attachments copied from the record path, with their recorded sequence. */
export interface SkeletonContent {
  readonly text?: Readonly<Record<string, string>>;
  readonly replies?: Readonly<
    Record<string, readonly {readonly seq: bigint; readonly hash: string}[]>
  >;
}
export type StoreHistory = Pick<
  Keyframe,
  'spans' | 'epochs' | 'streams' | 'lifecycles' | 'metrics'
> & {readonly throughSeq: bigint | undefined};
export interface StoreView {
  readonly seq: bigint;
  readonly spans: Readonly<Record<string, Span>>;
  readonly metrics: readonly Pick<MetricHistory, 'series' | 'points'>[];
  readonly epochs: readonly EpochVersion[];
  readonly lifecycle: Lifecycle | undefined;
}
