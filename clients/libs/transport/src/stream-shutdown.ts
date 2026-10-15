/** One asynchronously closeable resource in a live-stream composition. */
interface AsyncCloseable {
  close(): Promise<void>;
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
