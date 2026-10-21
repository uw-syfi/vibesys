import type {EpochVersion, Keyframe, Lifecycle, MetricHistory, Span} from '@vibesys/proto';
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
