import type {Keyframe} from '@vibesys/proto';

/** Binary search requires strictly ordered histories, not just decodable protobuf bytes. */
function ordered(versions: readonly {seq: bigint}[], throughSeq: bigint, path: string): void {
  let previous = 0n;
  for (const version of versions) {
    if (version.seq <= previous || version.seq > throughSeq)
      throw new Error(`${path}.seq: expected increasing sequence within through_seq`);
    previous = version.seq;
  }
}

export function validateKeyframe(keyframe: Keyframe): void {
  if (keyframe.throughSeq <= 0n) throw new Error('keyframe.through_seq: must be positive');
  for (const [id, history] of Object.entries(keyframe.spans)) {
    ordered(history.versions, keyframe.throughSeq, `keyframe.spans[${id}]`);
    for (const version of history.versions) {
      if (!version.span || version.span.id !== id)
        throw new Error(`keyframe.spans[${id}].span: missing or mismatched id`);
    }
  }
  validateMetrics(keyframe);
  ordered(keyframe.epochs, keyframe.throughSeq, 'keyframe.epochs');
  ordered(keyframe.lifecycles, keyframe.throughSeq, 'keyframe.lifecycles');
  if (keyframe.epochs.some(version => !version.epoch))
    throw new Error('keyframe.epochs.epoch: required');
  if (keyframe.lifecycles.some(version => !version.lifecycle))
    throw new Error('keyframe.lifecycles.lifecycle: required');
  const streams = new Set<string>();
  for (const stream of keyframe.streams) {
    if (streams.has(stream.id)) throw new Error(`keyframe.streams: duplicate id ${stream.id}`);
    streams.add(stream.id);
  }
}

function validateMetrics(keyframe: Keyframe): void {
  const series = new Set<string>();
  for (const history of keyframe.metrics) {
    if (series.has(history.series))
      throw new Error(`keyframe.metrics: duplicate series ${history.series}`);
    series.add(history.series);
    ordered(history.points, keyframe.throughSeq, `keyframe.metrics[${history.series}]`);
    for (const version of history.points) {
      if (!version.point || version.point.series !== history.series)
        throw new Error(`keyframe.metrics[${history.series}].point: missing or mismatched series`);
    }
  }
}
