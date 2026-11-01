/** Native fixture data comes from the shipping pure projection, once per derived replay set. */
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {fromBinary, type MessageShape} from '@bufbuild/protobuf';
import {ClientReplaySchema, type Record as StoreRecord} from '@vibesys/proto';
import type {RunFixture} from '@vibesys/run-fixtures';
import {derivedVariantsDirectory} from '@vibesys/run-fixtures/derived';
import {declaredReplay, ROOT, recordsOf} from '../src/testing/native-records.js';
import {
  connectUpdates,
  type NativeFixtureOptions,
  replayWorld,
} from '../src/testing/replay-world.js';
import {gatewayScript} from './fake-gateway.js';

type Replay = MessageShape<typeof ClientReplaySchema>;

const projector = join(ROOT, 'tests/e2e/retained_client_fixture.py');

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
      execFileSync('uv', ['run', '--locked', '--project', ROOT, 'python', projector, variants], {
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

/** The replay of `fixture`: the one generated from its declaration, else the one projected for it. */
function replayFor(fixture: RunFixture) {
  return declaredReplay(fixture) ?? variantReplay(fixture);
}

/** Every record the shipping projection makes of the fixture's run, in log order. */
export function nativeRecords(fixture: RunFixture): StoreRecord[] {
  return recordsOf(replayFor(fixture));
}

export type {NativeFixtureOptions};

export function nativeFixtureScript(
  fixture: RunFixture,
  options: NativeFixtureOptions = {},
): string {
  return nativeReplayScript(fixture, replayFor(fixture), options);
}

/** Serve Python-produced records through the same gateway for recorded and declarative fixtures. */
export function nativeReplayScript(
  fixture: RunFixture,
  replay: Replay,
  options: NativeFixtureOptions = {},
): string {
  const {input, updates} = replayWorld(fixture, replay, options);
  return `if (!window.vibesysGateway) {${gatewayScript(input)}\n(${connectUpdates.toString()})(${JSON.stringify(updates)});}`;
}
