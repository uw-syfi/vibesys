/** Callback failures belong to the test even when the product catches them for retry. */
const errors: unknown[] = [];

export function invokeCallback<A>(callback: () => A): A {
  try {
    return callback();
  } catch (error) {
    errors.push(error);
    throw error;
  }
}

/** Called after every test, outside all product retry and fiber boundaries. */
export function assertNoCallbackErrors(): void {
  const failed = errors.splice(0);
  if (failed.length > 0) throw new AggregateError(failed, 'Fake callback failed');
}
