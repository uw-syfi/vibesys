/**
 * The hosts the user saved, read from the app's user data so that the window opens on them. Only
 * the keys matter here: this app reads them and never writes them. A file is read in its current
 * form (`hosts.pb`, protobuf); the older JSON form is read when only it exists. A file that cannot
 * be read or parsed yields no hosts and a log line naming it, so a damaged file never keeps the
 * window from opening.
 */
import {join} from 'node:path';
import {RetainedDesktopHostsSchema, validatedFromBinary} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import {type HostKey, hostFromKey, hostKey} from '../state/host.js';
import type {ReadFile} from './files.js';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const keysOfJson = (text: string): readonly string[] => {
  const document: unknown = JSON.parse(text);
  if (!isRecord(document) || !isRecord(document['hosts'])) throw new Error('no hosts object');
  return Object.keys(document['hosts']);
};

/** The saved host keys under `userData`, valid ones only. */
export const savedHosts = (
  userData: string,
  read: ReadFile,
  log: (line: string) => void,
): Effect.Effect<readonly HostKey[]> =>
  Effect.gen(function* () {
    const binary = join(userData, 'hosts.pb');
    const json = join(userData, 'hosts.json');
    let keys: readonly string[] = [];
    let source = binary;
    const bytes = yield* read(binary);
    if (bytes !== null) {
      keys = Object.keys(validatedFromBinary(RetainedDesktopHostsSchema, bytes).hosts);
    } else {
      source = json;
      const text = yield* read(json);
      if (text !== null) keys = keysOfJson(new TextDecoder().decode(text));
    }
    const valid: HostKey[] = [];
    for (const key of keys) {
      try {
        valid.push(hostKey(hostFromKey(key)));
      } catch (cause) {
        log(`${source}: ignoring host ${JSON.stringify(key)}: ${String(cause)}`);
      }
    }
    return valid;
  }).pipe(
    Effect.catchCause(cause =>
      Effect.sync(() => {
        log(`could not read the saved hosts: ${String(cause)}`);
        return [] as readonly HostKey[];
      }),
    ),
  );
