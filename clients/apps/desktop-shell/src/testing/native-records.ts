/**
 * The records the shipping projection makes of a run fixture, once per derived replay set: the
 * data the native gateway Fake serves, for the browser journeys (`e2e/native-fixture.ts`) and for
 * the DOM tests of the Run screen. It imports nothing of Playwright.
 */
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {fromBinary, type MessageShape} from '@bufbuild/protobuf';
import {ClientReplaySchema, RecordSchema, type Record as StoreRecord} from '@vibesys/proto';
import {parseFixture, type RunFixture, type StoreRunFixture} from '@vibesys/run-fixtures';
import {
  type DerivedReplaySet,
  derivedReplay,
  derivedVariantsDirectory,
} from '@vibesys/run-fixtures/derived';

const root = fileURLToPath(new URL('../../../../../', import.meta.url));
const projector = join(root, 'tests/e2e/retained_client_fixture.py');

function hash(bytes: string | Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex');
}

/**
 * The replay of a fixture that differs from its declaration, projected once and kept inside the
 * derived set, so it is exactly as current as the canonical replays beside it.
 */
function variantReplay(fixture: RunFixture) {
  const variants = derivedVariantsDirectory();
  const key = hash(JSON.stringify(fixture));
  if (!existsSync(join(variants, key, '.complete'))) {
    try {
      execFileSync('uv', ['run', '--locked', '--project', root, 'python', projector, variants], {
        input: JSON.stringify([{key, fixture}]),
        maxBuffer: 16 << 20,
        timeout: 120000,
      });
    } catch (error) {
      throw new Error(
        `cannot project the replay of fixture ${fixture.name} (it differs from its declaration): ` +
          `${error instanceof Error ? error.message : error}. The Linux image has no uv; run the ` +
          'spec once on the host so the replay is kept in the derived replay set.',
        {cause: error},
      );
    }
  }
  return fromBinary(ClientReplaySchema, readFileSync(join(variants, key, 'replay.pb')));
}

/**
 * A fixture equal to a declaration reads the replay generated from it, and a set that lacks that
 * replay fails naming it; any other fixture is projected by the same real projector.
 */
export function replayFor(fixture: RunFixture): MessageShape<typeof ClientReplaySchema> {
  const sources: readonly {data: string; set: DerivedReplaySet}[] = [
    {data: join(root, 'clients/libs/run-fixtures/src/data'), set: 'shared'},
    {data: join(root, 'clients/apps/desktop-shell/e2e/scenarios'), set: 'journeys'},
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
  return variantReplay(fixture);
}
/** Every record the shipping projection makes of the fixture's run, in log order. */
export function nativeRecords(fixture: RunFixture): StoreRecord[] {
  return replayFor(fixture)
    .batches.flatMap(batch => batch.records.map(bytes => fromBinary(RecordSchema, bytes)))
    .sort((a, b) => Number(a.seq - b.seq));
}

/** The run `fixture` is once it has ended, or at its end, as the store holds it. */
export function nativeStoreFixture(fixture: RunFixture): StoreRunFixture {
  const replay = replayFor(fixture);
  const records = nativeRecords(fixture);
  return {records, initialCount: records.length, blobs: replay.blobs, streams: replay.streams};
}
