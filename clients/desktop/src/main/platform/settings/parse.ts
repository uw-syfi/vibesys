/** The settings files' shared parsing: objects with known keys only, failing with the path. */

/** A settings file that cannot be read as this version writes it; the message names the path. */
export class SettingsError extends Error {
  override name = 'SettingsError';
}

export function object(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SettingsError(`${path} must be an object`);
  }
  return value as Record<string, unknown>;
}

export function onlyKeys(
  record: Record<string, unknown>,
  keys: readonly string[],
  path: string,
): void {
  for (const key of Object.keys(record)) {
    if (!keys.includes(key)) throw new SettingsError(`${path}.${key} is not a known field`);
  }
}
