import type {BorderStyle, BoxRenderable} from '@opentui/core';
import type {Theme} from './theme.js';

/**
 * How a pane says it is the one taking keys.
 *
 * Two channels, in priority order: a marker in a reserved title gutter, then
 * colour. Colour is second because it cannot carry the signal on its own.
 * `borderFocus` and `border` sit within 1.5x of each other in five of the eight
 * built-in themes, and the high-contrast pair collapses them by construction:
 * those palettes are deliberately tiny, so there is no shade to move to. The
 * marker holds in any palette, and in a terminal with no colour at all. Frame
 * weight is not a third channel; `PANE_BORDER` records why it was dropped.
 *
 * This module owns how focus looks, never which surface has it: `focusedPane`
 * is that authority, and each pane compares its own `PaneId` against it. A
 * surface that is not a pane never wears the treatment, and neither does a box
 * inside a pane that already does: two lit frames, one nested in the other, are
 * as ambiguous as two lit panes side by side. Both boxes that take typed text
 * are that case: the chat's `Message` composer sits inside the chat pane, and
 * the command box sits inside the pane whose keys it takes. Both take the
 * resting title, frame and colour and no focused variant, and call in only to
 * keep their labels at the same column as the pane they are drawn in. Lighting
 * the command box made the marked pane ambiguous, which is #433.
 */

/** Marks the title of the pane that currently takes keys. */
const FOCUS_MARKER = '▸';

/** Occupies the marker's cell while a pane is at rest. */
const MARKER_GUTTER = ' ';

/**
 * Every pane keeps the rounded frame, focused or not.
 *
 * A heavier frame was tried as a second non-colour channel and dropped: the
 * only heavier box-drawing weights are square (`┏┓┗┛`), because Unicode has no
 * heavy rounded corner. The rounded arcs at U+256D-2570 are light-only. So the
 * swap traded rounded corners for square ones, which is a change of shape
 * rather than of weight, and reads as the pane becoming a different kind of
 * object rather than a louder one.
 *
 * That leaves the reserved title marker as the non-colour channel. It is
 * enough for WCAG 1.4.1, and unlike a frame weight it cannot be mistaken for a
 * different component.
 */
const PANE_BORDER: BorderStyle = 'rounded';

/**
 * A pane title with the marker's cell reserved whether or not the pane holds
 * focus.
 *
 * Prepending the marker instead slides the label two columns right, so the eye,
 * which uses the title's left edge as a landmark, reads a focus change as the
 * layout moving. Reserving the cell turns it into a glyph swap at a fixed
 * column, and keeps a narrow pane's title truncating identically either way.
 */
export function paneTitle(label: string, focused: boolean): string {
  // Symmetric: the gutter and the lead space together are two cells, and the
  // label gets two on the other side, so the label sits centred in its own
  // inset rather than pushed against the trailing edge.
  return ` ${focused ? FOCUS_MARKER : MARKER_GUTTER} ${label}   `;
}

/**
 * The frame a pane draws.
 *
 * The same frame at rest and in focus, which is why the parameter is unused. It
 * is kept so callers read the same shape as the other helpers here, and so a
 * future palette that can afford a third channel has somewhere to put it.
 */
export function paneBorderStyle(_focused: boolean): BorderStyle {
  return PANE_BORDER;
}

/** Reinforces the marker above, as far as a given palette allows. */
export function paneBorderColor(theme: Theme, focused: boolean): string {
  return focused ? theme.borderFocus : theme.border;
}

/** Applies every focus channel to one pane frame. */
export function applyPaneFocus(
  box: BoxRenderable,
  theme: Theme,
  label: string,
  focused: boolean,
): void {
  box.title = paneTitle(label, focused);
  box.borderStyle = paneBorderStyle(focused);
  box.borderColor = paneBorderColor(theme, focused);
}
