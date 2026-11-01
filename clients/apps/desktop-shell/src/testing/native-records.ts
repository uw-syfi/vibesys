/**
 * The records the shipping projection makes of a run fixture, read from the derived replay set the
 * run started with: the data the native gateway Fake serves, for the browser journeys
 * (`e2e/native-fixture.ts`) and for the DOM tests of the Run screen. It imports nothing of
 * Playwright and starts no process: a fixture that differs from its declaration has no replay here,
 * and the browser journeys project it (`e2e/native-fixture.ts`).
 */
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {fromBinary, type MessageShape} from '@bufbuild/protobuf';
import {ClientReplaySchema, RecordSchema, type Record as StoreRecord} from '@vibesys/proto';
import {parseFixture, type RunFixture, type StoreRunFixture} from '@vibesys/run-fixtures';
import {type DerivedReplaySet, derivedReplay} from '@vibesys/run-fixtures/derived';

type Replay = MessageShape<typeof ClientReplaySchema>;

export const ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));

/**
 * The replay generated from the declaration `fixture` equals, or `undefined` when it equals none
 * (a set that lacks the replay of a declaration fails naming it).
 */
export function declaredReplay(fixture: RunFixture): Replay | undefined {
  const sources: readonly {data: string; set: DerivedReplaySet}[] = [
    {data: join(ROOT, 'clients/libs/run-fixtures/src/data'), set: 'shared'},
    {data: join(ROOT, 'clients/apps/desktop-shell/e2e/scenarios'), set: 'journeys'},
  ];
  for (const source of sources) {
    const declaration = join(source.data, `${fixture.name}.json`);
    if (!existsSync(declaration)) continue;
    const {
      lineage: _lineage,
      nativeLifecycles: _lifecycles,
      nativeStreams: _streams,
      nativeTransitions: _transitions,
      ...value
    } = JSON.parse(readFileSync(declaration, 'utf8'));
    // A JSON file without events is data for something else, not a declaration with a replay.
    if (!('events' in value)) continue;
    if (JSON.stringify(fixture) === JSON.stringify(parseFixture(value))) {
      return fromBinary(ClientReplaySchema, derivedReplay(source.set, fixture.name));
    }
  }
  return undefined;
}

/** Every record of `replay`, in log order. */
export function recordsOf(replay: Replay): StoreRecord[] {
  return replay.batches
    .flatMap(batch => batch.records.map(bytes => fromBinary(RecordSchema, bytes)))
    .sort((a, b) => Number(a.seq - b.seq));
}

/** The run `fixture` is at its end, as the store holds it. */
export function nativeStoreFixture(fixture: RunFixture): StoreRunFixture {
  const replay = declaredReplay(fixture);
  if (replay === undefined) {
    throw new Error(
      `fixture ${fixture.name} differs from its declaration: it has no derived replay`,
    );
  }
  const records = recordsOf(replay);
  return {records, initialCount: records.length, blobs: replay.blobs, streams: replay.streams};
}
