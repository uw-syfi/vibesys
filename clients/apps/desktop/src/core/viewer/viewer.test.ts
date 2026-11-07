import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import type * as Layer from 'effect/Layer';
import fc from 'fast-check';
import {makeFakeViewerBridge} from '../../testing/viewer/fakes.js';
import {Viewer, viewerOver} from './viewer.js';
import {type ViewerIdentity, viewerFromBytes, viewerToBytes} from './viewer-codec.js';

const whoami = (layer: Layer.Layer<Viewer>) =>
  Effect.gen(function* () {
    const viewer = yield* Viewer;
    return yield* viewer.whoami;
  }).pipe(Effect.provide(layer));

/** Any id and label the contract allows: an id of safe characters, a label of printable ASCII. */
const viewerArb: fc.Arbitrary<ViewerIdentity> = fc.record({
  id: fc.stringMatching(/^[A-Za-z0-9._-]{1,128}$/),
  label: fc.stringMatching(/^[!-~]([ -~]{0,126}[!-~])?$/),
});

describe('the viewer', () => {
  it('survives the bytes it crosses the bridge as, for any id and label the contract allows', () => {
    fc.assert(
      fc.property(viewerArb, viewer => {
        expect(viewerFromBytes(viewerToBytes(viewer))).toEqual(viewer);
      }),
      {numRuns: 200},
    );
  });

  it('refuses an id or a label the contract does not allow, going out and coming in', () => {
    for (const bad of [
      {id: '', label: 'ana'},
      {id: 'has space', label: 'ana'},
      {id: 'x'.repeat(129), label: 'ana'},
      {id: 'install', label: ''},
      {id: 'install', label: 'tab\there'},
      {id: 'install', label: ' padded '},
      {id: 'install', label: 'café'},
      {id: 'install', label: 'x'.repeat(129)},
    ]) {
      expect(() => viewerToBytes(bad)).toThrow();
    }
    expect(() => viewerFromBytes(new Uint8Array([0xff, 0xff, 0xff]))).toThrow();
  });

  it.effect('is read from the bridge', () =>
    Effect.gen(function* () {
      const fake = makeFakeViewerBridge({viewer: {id: 'install-7', label: 'Sam'}});
      expect(yield* whoami(viewerOver(fake.bridge))).toEqual({id: 'install-7', label: 'Sam'});
      expect(fake.reads()).toBe(1);
    }),
  );

  it.effect(
    'fails with the reason when the bridge does, sends bytes that are no viewer, or is absent',
    () =>
      Effect.gen(function* () {
        const cases = [
          [viewerOver(makeFakeViewerBridge({failRead: 'denied'}).bridge), 'denied'],
          [viewerOver(makeFakeViewerBridge({bytes: new Uint8Array([1, 2, 3])}).bridge), ''],
          [viewerOver(undefined), 'no viewer bridge'],
        ] as const;
        for (const [layer, detail] of cases) {
          const failure = yield* Effect.flip(whoami(layer));
          expect(failure._tag).toBe('ViewerError');
          expect(failure.detail).toContain(detail);
          expect(failure.detail).not.toBe('');
        }
      }),
  );
});
