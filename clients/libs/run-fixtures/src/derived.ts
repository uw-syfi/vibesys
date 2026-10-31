/**
 * The native replays that Python generates from the declarations of this package and of the desktop
 * shell scenarios (`tests/support/derived_replays.py`). They are not committed: a run that needs
 * them starts through `clients/tools/scripts/with_derived_replays.mjs`, which makes the set the
 * current inputs name and exports its directory. This is the one reader of that directory, so a
 * run that skipped the script fails naming it instead of reading a stale or missing file.
 */
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';

export const DERIVED_REPLAYS_VARIABLE = 'VIBESYS_DERIVED_REPLAYS';

/** `shared` holds the replays of this package's declarations; `journeys` those of the desktop shell's. */
export type DerivedReplaySet = 'shared' | 'journeys';

function directory(): string {
  const value = process.env[DERIVED_REPLAYS_VARIABLE];
  if (!value) {
    throw new Error(
      `${DERIVED_REPLAYS_VARIABLE} is not set: run through the package script (pnpm test, pnpm e2e), ` +
        'which runs clients/tools/scripts/with_derived_replays.mjs first',
    );
  }
  return value;
}

/**
 * Where the replays of fixtures that differ from their declaration are kept. It sits inside the
 * set, so the key that names the set also covers them and they are never older than it.
 */
export function derivedVariantsDirectory(): string {
  return join(directory(), 'variants');
}

/** The bytes of the replay generated for a declaration; throws naming the missing file. */
export function derivedReplay(set: DerivedReplaySet, name: string): Uint8Array {
  const path = join(directory(), set, `${name}.pb`);
  if (!existsSync(path)) {
    throw new Error(`no derived replay ${set}/${name}.pb in ${directory()}; is ${name} declared?`);
  }
  return readFileSync(path);
}
