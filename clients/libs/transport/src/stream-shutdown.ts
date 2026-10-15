/** One asynchronously closeable resource in a live-stream composition. */
interface AsyncCloseable {
  close(): Promise<void>;
}

/** The outcome of one observer delivery; the boundary itself never throws. */
export type ObserverDelivery = {readonly ok: true} | {readonly ok: false; readonly reason: unknown};

/**
 * Contain notification callbacks until their owner's mandatory terminal join.
 *
 * An observer does not own the transport transition that calls it, so its
 * failure cannot interrupt socket fencing or retirement. `capture` records the
 * exact thrown value and returns it to the owner when that failure also means
 * the current operation must retire. `settle` is the asynchronous error
 * boundary: it waits for every callback already in progress (including one
 * that calls terminal close reentrantly), preserves one failure by identity,
 * and aggregates several in delivery order. Repeated settlement shares one
 * promise and therefore one aggregate identity.
 */
export class ObserverFailureBoundary {
  readonly #failures: unknown[] = [];
  #active = 0;
  #settling: Promise<void> | null = null;
  #resolve = (): void => {};
  #reject = (_reason: unknown): void => {};

  capture(callback: () => void): ObserverDelivery {
    this.#active += 1;
    try {
      callback();
      return {ok: true};
    } catch (reason) {
      this.#failures.push(reason);
      return {ok: false, reason};
    } finally {
      this.#active -= 1;
      this.#finish();
    }
  }

  /** Join active deliveries, then expose their retained failures exactly once. */
  settle(): Promise<void> {
    if (this.#settling !== null) return this.#settling;
    this.#settling = new Promise<void>((resolve, reject) => {
      this.#resolve = resolve;
      this.#reject = reject;
    });
    this.#finish();
    return this.#settling;
  }

  #finish(): void {
    if (this.#settling === null || this.#active !== 0) return;
    if (this.#failures.length === 0) {
      this.#resolve();
      return;
    }
    if (this.#failures.length === 1) {
      this.#reject(this.#failures[0]);
      return;
    }
    this.#reject(
      new AggregateError(this.#failures, 'multiple transport observer callbacks failed'),
    );
  }
}

/** Begin one close without letting a synchronous throw prevent the peer close from starting. */
function beginClose(resource: AsyncCloseable): Promise<void> {
  try {
    return resource.close();
  } catch (error) {
    return Promise.reject(error);
  }
}

/**
 * Await every already-started settlement before exposing its failures.
 *
 * One failure is preserved verbatim. Several become one `AggregateError` of
 * their distinct leaf failures in input order. Flattening and identity
 * deduplication matter when an outer owner joins both a composite child close
 * and one of that child's resources: the same failure is evidence once, not a
 * new failure at every ownership layer.
 */
export async function settleAll(
  settlements: readonly Promise<void>[],
  aggregateMessage: string,
): Promise<void> {
  const results = await Promise.allSettled(settlements);
  const failures = results.flatMap(result => (result.status === 'rejected' ? [result.reason] : []));
  if (failures.length === 0) return;
  if (failures.length === 1) throw failures[0];
  const leaves: unknown[] = [];
  const seen = new Set<unknown>();
  const append = (failure: unknown): void => {
    if (failure instanceof AggregateError) {
      for (const nested of failure.errors as unknown[]) append(nested);
      return;
    }
    if (seen.has(failure)) return;
    seen.add(failure);
    leaves.push(failure);
  };
  for (const failure of failures) append(failure);
  if (leaves.length === 1) throw leaves[0];
  throw new AggregateError(leaves, aggregateMessage);
}

/**
 * Fence an event stream, then cancel and settle its transport.
 *
 * Calling the stream first is significant: an async `PersistentEventStream.close()` fences
 * delivery synchronously before its first await. Starting the transport close immediately after
 * that cancels a subscribe handshake the stream may be awaiting. Both resources still settle,
 * even when either close fails; a sole failure is preserved and two failures retain their stable
 * stream-then-transport order.
 */
export async function closeStreamAndTransport(
  stream: AsyncCloseable,
  transport: AsyncCloseable,
): Promise<void> {
  await settleAll(
    [beginClose(stream), beginClose(transport)],
    'could not close event stream and transport',
  );
}
