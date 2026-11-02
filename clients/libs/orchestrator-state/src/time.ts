import type {Timestamp} from '@bufbuild/protobuf/wkt';

/** Milliseconds since the epoch; an absent timestamp is `null`, never zero. */
export function ms(at: Timestamp | undefined): number | null {
  return at === undefined ? null : Number(at.seconds) * 1000 + Math.floor(at.nanos / 1_000_000);
}
