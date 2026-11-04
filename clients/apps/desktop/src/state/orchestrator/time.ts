/** A protobuf `Timestamp`, by shape: the fold reads the two fields and names no library for them. */
export interface Instant {
  readonly seconds: bigint;
  readonly nanos: number;
}

/** Milliseconds since the epoch; an absent timestamp is `null`, never zero. */
export function ms(at: Instant | undefined): number | null {
  return at === undefined ? null : Number(at.seconds) * 1000 + Math.floor(at.nanos / 1_000_000);
}
