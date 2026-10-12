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
  const results = await Promise.allSettled([beginClose(stream), beginClose(transport)]);
  const failures = results.flatMap(result => (result.status === 'rejected' ? [result.reason] : []));
  if (failures.length === 0) return;
  if (failures.length === 1) throw failures[0];
  throw new AggregateError(failures, 'could not close event stream and transport');
}
