/**
 * What the New run screen holds of one host answer (the catalogue, the eval places). The data a host
 * sent stays while it is read again, so the screen dims it until one swap; a read that failed keeps
 * the words of the failure beside what was there before.
 */
export interface Remote<A> {
  /** The latest answer, or null before the first. */
  readonly data: A | null;
  /** A read is under way: `data`, if any, is the previous answer. */
  readonly reading: boolean;
  /** Why the latest read failed, or null. */
  readonly failure: string | null;
}

export const NOT_READ: Remote<never> = {data: null, reading: false, failure: null};

/** The state when a read starts: the data stays, the failure clears. */
export const readingOf = <A>(before: Remote<A>): Remote<A> => ({
  data: before.data,
  reading: true,
  failure: null,
});

/** The state when a read answers: one swap to the new data. */
export const readOf = <A>(data: A): Remote<A> => ({data, reading: false, failure: null});

/** The state when a read fails: the old data stays, with the reason. */
export const failedOf = <A>(before: Remote<A>, failure: string): Remote<A> => ({
  data: before.data,
  reading: false,
  failure,
});
