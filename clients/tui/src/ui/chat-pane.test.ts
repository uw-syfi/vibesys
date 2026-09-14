import {describe, expect, it} from 'bun:test';
import {
  CHAT_PANE_MAX,
  chatDockFits,
  chatPaneWidth,
  chatPaneWidthWithOverride,
  clampChatWidthOverride,
  MIN_DOCK_WIDTH,
} from './chat-pane.js';
import {LOG_CLAIM_PANEL_WIDTH, LOG_COMPACT_PANEL_WIDTH} from './experiment-log.js';

describe('chat dock thresholds', () => {
  it('docks wherever the table is still usable beside it', () => {
    expect(chatDockFits(MIN_DOCK_WIDTH)).toBe(true);
    expect(chatDockFits(MIN_DOCK_WIDTH - 1)).toBe(false);
    // Small enough that two columns would both be unreadable.
    expect(chatDockFits(80)).toBe(false);
    expect(chatDockFits(120)).toBe(true);
  });

  it('docks beside a visualization only when all three columns fit', () => {
    expect(chatDockFits(200, 84)).toBe(true);
    // The visualization has taken the room the chat would need.
    expect(chatDockFits(140, 63)).toBe(false);
  });
});

describe('chat dock sizing', () => {
  it('takes the columns left over once the table has its claim', () => {
    // No surplus: the chat takes its minimum and the table keeps the rest.
    expect(chatPaneWidth(MIN_DOCK_WIDTH)).toBe(25);
    expect(chatPaneWidth(200)).toBeGreaterThan(chatPaneWidth(140));
  });

  it('stops widening once the chat is comfortable', () => {
    expect(chatPaneWidth(400)).toBe(chatPaneWidth(300));
  });

  it('costs the table no column once there are spare ones', () => {
    for (let width = LOG_CLAIM_PANEL_WIDTH + 25; width <= 400; width += 1) {
      const log = width - chatPaneWidth(width);
      expect(log, `log at ${width}`).toBeGreaterThanOrEqual(LOG_CLAIM_PANEL_WIDTH);
    }
  });

  it('never takes the table below its compact set', () => {
    for (let width = MIN_DOCK_WIDTH; width <= 400; width += 1) {
      const log = width - chatPaneWidth(width);
      expect(log, `log at ${width}`).toBeGreaterThanOrEqual(LOG_COMPACT_PANEL_WIDTH);
    }
    const right = 84;
    for (let width = 200; width <= 400; width += 1) {
      const log = width - right - chatPaneWidth(width, right);
      expect(log, `log at ${width} beside a pane`).toBeGreaterThanOrEqual(LOG_COMPACT_PANEL_WIDTH);
    }
  });

  it('narrows itself rather than the table when a visualization opens', () => {
    expect(chatPaneWidth(200, 84)).toBeLessThan(chatPaneWidth(200));
  });
});

/**
 * The range a `<`/`>` override is clamped to. Deliberately asymmetric with
 * automatic sizing: the low bound is the same `CHAT_PANE_MIN` automatic sizing
 * never crosses, but the high bound is whatever the log's own compact floor
 * leaves over, not `CHAT_PANE_MAX`. Asking for more than automatic sizing
 * would ever pick is the point of overriding it.
 */
describe('clampChatWidthOverride', () => {
  it('holds the override between CHAT_PANE_MIN and what the log floor leaves over', () => {
    const width = 300; // room = 233, far past CHAT_PANE_MAX.
    expect(clampChatWidthOverride(-1000, width, 0)).toBe(25);
    expect(clampChatWidthOverride(1000, width, 0)).toBe(width - LOG_COMPACT_PANEL_WIDTH);
    expect(clampChatWidthOverride(60, width, 0)).toBe(60);
  });

  it('is allowed past CHAT_PANE_MAX, unlike automatic sizing', () => {
    const width = 300;
    const overridden = clampChatWidthOverride(1000, width, 0);
    expect(overridden).toBeGreaterThan(CHAT_PANE_MAX);
    // The log still keeps its compact floor: the extra columns come out of
    // the chat's own ceiling, never out of the log's.
    expect(width - overridden).toBeGreaterThanOrEqual(LOG_COMPACT_PANEL_WIDTH);
  });

  it('takes the same columns off a visualization split that automatic sizing does', () => {
    const width = 300;
    const right = 84;
    expect(clampChatWidthOverride(1000, width, right)).toBe(
      width - right - LOG_COMPACT_PANEL_WIDTH,
    );
  });
});

describe('chatPaneWidthWithOverride', () => {
  it('with no override, matches automatic sizing exactly', () => {
    for (const width of [92, 100, 140, 160, 300]) {
      expect(chatPaneWidthWithOverride(width, 0, null)).toBe(chatPaneWidth(width, 0));
    }
  });

  it('with an override, matches the clamp exactly', () => {
    expect(chatPaneWidthWithOverride(300, 0, 1000)).toBe(clampChatWidthOverride(1000, 300, 0));
    expect(chatPaneWidthWithOverride(300, 0, -1000)).toBe(clampChatWidthOverride(-1000, 300, 0));
  });
});
