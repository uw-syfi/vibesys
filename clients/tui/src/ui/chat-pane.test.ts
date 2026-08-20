import {describe, expect, it} from 'bun:test';
import {chatDockFits, chatPaneWidth, MIN_DOCK_WIDTH} from './chat-pane.js';
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
