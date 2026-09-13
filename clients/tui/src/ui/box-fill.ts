import {BoxRenderable} from '@opentui/core';

/**
 * Paints `color` across the inside of `box`, up to but not over its border.
 *
 * A `backgroundColor` on the box itself cannot do that. `BoxRenderable`
 * forwards it to `OptimizedBuffer.drawBox` as one background for the whole
 * rectangle, and the buffer is write-only, so every cell takes the fill and the
 * border glyph is drawn on top of it. The painted rectangle is then one cell
 * larger than the drawn line on all four sides: the line sits in a solid block
 * with fill on both sides of it, and under a rounded arc the fill paints the
 * outside of the curve and squares the corner back off. That is #642.
 *
 * A separate box carrying the fill leaves the border ring showing whatever the
 * box sits on, so a cell reads canvas, then line, then fill, and a corner cell
 * has no fill in it to bleed past the arc.
 *
 * Absolutely positioned and inserted first rather than wrapped around the
 * content, so it adds no row, no padding and no containing block and the box's
 * children stay exactly where they were. It carries no mouse handlers, and
 * OpenTUI bubbles a mouse event to the parent, so it does not take clicks off
 * the box it fills.
 */
export function fillLayer(box: BoxRenderable, id: string, color: string): BoxRenderable {
  const fill = new BoxRenderable(box.ctx, {
    id,
    position: 'absolute',
    // The four insets are measured from the padding box, which is the box
    // inside its border, so this is exactly the interior and nothing else.
    left: 0,
    right: 0,
    top: 0,
    bottom: 0,
    backgroundColor: color,
  });
  // First, so every child added later paints over it.
  box.add(fill, 0);
  return fill;
}
