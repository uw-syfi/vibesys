import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  afterWindow,
  LIVENESS_WINDOW_MS,
  SILENCE_BOUND_MS,
  SILENT_WINDOWS_TO_LOSE,
} from './liveness.js';

/**
 * The monitor of a session, played against a timeline of the instants bytes arrived: windows end
 * every `LIVENESS_WINDOW_MS`, each tells whether any instant fell inside it, and the first `lost`
 * ends the session. Answers when the session was declared lost (null: it never was by `until`).
 */
const lostAt = (arrivals: readonly number[], until: number): number | null => {
  let quiet = 0;
  for (let end = LIVENESS_WINDOW_MS; end <= until; end += LIVENESS_WINDOW_MS) {
    const heard = arrivals.some(at => at > end - LIVENESS_WINDOW_MS && at <= end);
    const step = afterWindow(heard, quiet);
    quiet = step.silent;
    if (step.action === 'lost') return end;
  }
  return null;
};

/** Instants in a 3-minute span, sorted. */
const timeline = fc
  .array(fc.integer({min: 1, max: 180_000}), {maxLength: 60})
  .map(instants => [...instants].sort((a, b) => a - b));

describe('link liveness', () => {
  it('probes the first silent window and loses the next, and any bytes start the count over', () => {
    expect(afterWindow(true, 0)).toEqual({silent: 0, action: 'none'});
    expect(afterWindow(false, 0)).toEqual({silent: 1, action: 'probe'});
    expect(afterWindow(false, 1)).toEqual({silent: 2, action: 'lost'});
    expect(afterWindow(true, 1)).toEqual({silent: 0, action: 'none'});
  });

  it('declares a session lost exactly when two windows in a row heard nothing', () => {
    fc.assert(
      fc.property(fc.array(fc.boolean(), {maxLength: 40}), windows => {
        let quiet = 0;
        let run = 0;
        for (const heard of windows) {
          const step = afterWindow(heard, quiet);
          quiet = step.silent;
          run = heard ? 0 : run + 1;
          expect(step.action === 'lost').toBe(run >= SILENT_WINDOWS_TO_LOSE);
          expect(step.action === 'probe').toBe(run > 0 && run < SILENT_WINDOWS_TO_LOSE);
          if (step.action === 'lost') return;
        }
      }),
    );
  });

  it('never loses a link that delivers within every window, however slowly', () => {
    fc.assert(
      fc.property(
        // Gaps under one window: a transfer that trickles in, a run that talks now and then.
        fc.array(fc.integer({min: 1, max: LIVENESS_WINDOW_MS - 1}), {minLength: 1, maxLength: 60}),
        gaps => {
          const arrivals = gaps.map((_, i) => gaps.slice(0, i + 1).reduce((a, b) => a + b, 0));
          const last = arrivals.at(-1) ?? 0;
          expect(lostAt(arrivals, last)).toBeNull();
        },
      ),
    );
  });

  it('detects every silence within the bound, whatever came before it', () => {
    fc.assert(
      fc.property(timeline, arrivals => {
        const last = arrivals.at(-1) ?? 0;
        const lost = lostAt(arrivals, last + SILENCE_BOUND_MS);
        expect(lost).not.toBeNull();
        expect((lost ?? 0) - last).toBeLessThanOrEqual(SILENCE_BOUND_MS);
      }),
    );
  });

  it('keeps its bound under a minute, short enough that a hung link is never shown as live for long', () => {
    expect(SILENCE_BOUND_MS).toBeLessThanOrEqual(15_000);
  });
});
