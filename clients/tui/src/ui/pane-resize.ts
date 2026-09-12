/**
 * Columns `<`/`>` step a resizable pane's width by on each press. The Agents
 * pane (`agent-map.ts`) and the docked chat pane (`chat-pane.ts`) share one
 * constant so the two keymaps read as the same convention rather than two
 * coincidentally equal numbers. 2 rather than 1: at 1 column a press would
 * often move only padding, not which characters of a label or a line are
 * visible.
 */
export const PANE_WIDTH_STEP = 2;
