/** The slice of a Playwright suite one CI job runs, in the shape Playwright's `shard` option takes. */
export interface E2eShard {
  readonly current: number;
  readonly total: number;
}

/**
 * The shard named by `variable` (`"2/5"` is the second of five), or `undefined` when it is unset or
 * empty, which runs the whole suite as a local run does. CI sets it so each job takes one slice of
 * every app's specs; together the slices run every spec exactly once. A malformed value fails the
 * run by name instead of silently running everything, which would hide a mis-sized matrix.
 */
export function e2eShard(
  variable: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): E2eShard | undefined {
  const value = env[variable];
  if (value === undefined || value === '') return undefined;
  const match = /^([1-9][0-9]*)\/([1-9][0-9]*)$/.exec(value);
  const current = Number(match?.[1]);
  const total = Number(match?.[2]);
  if (match === null || current > total) {
    throw new Error(`${variable} must look like "2/5" with 1 <= current <= total, got "${value}"`);
  }
  return {current, total};
}
