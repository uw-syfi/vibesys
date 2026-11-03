import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {UPLOAD_BYTES_PER_SECOND, UPLOAD_CHUNK_BYTES, uploadSchedule} from './upload-pacing.js';

describe('upload pacing', () => {
  it('covers every byte once, in order, in chunks of at most 32 KiB', () => {
    fc.assert(
      fc.property(fc.integer({min: 0, max: 5_000_000}), total => {
        const steps = uploadSchedule(total);
        let next = 0;
        for (const step of steps) {
          expect(step.offset).toBe(next);
          expect(step.length).toBeGreaterThan(0);
          expect(step.length).toBeLessThanOrEqual(UPLOAD_CHUNK_BYTES);
          next += step.length;
        }
        expect(next).toBe(total);
      }),
    );
  });

  it('never goes faster than the cap, over the whole upload or any prefix', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 5_000_000}), total => {
        let sent = 0;
        let waited = 0;
        for (const step of uploadSchedule(total)) {
          waited += step.delayMs;
          sent += step.length;
          expect(sent / (waited / 1000)).toBeLessThanOrEqual(UPLOAD_BYTES_PER_SECOND);
        }
      }),
    );
  });

  it('sends nothing for nothing', () => {
    expect(uploadSchedule(0)).toEqual([]);
  });
});
