import {BoxRenderable, type CliRenderer, hexToRgb, type RGBA} from '@opentui/core';
import {scrim, type Theme} from './theme.js';

/** The theme's scrim as a translucent paint the renderer blends per cell. */
function scrimPaint(theme: Theme): RGBA {
  const {color, strength} = scrim(theme);
  const paint = hexToRgb(color);
  paint.a = strength;
  return paint;
}

/**
 * The dim behind an open modal. One box covering the whole screen, painted
 * after everything below it and before every modal above it, so the entire
 * background recedes while the modal keeps its own colours and reads as the
 * only surface accepting keys.
 *
 * It is absolutely positioned and joins no flex row or column, and the paint is
 * translucent rather than opaque, so a cell behind it keeps its character and
 * changes only colour. Opening and closing the modal therefore moves nothing.
 */
export class ScrimView {
  readonly output: BoxRenderable;

  constructor(renderer: CliRenderer, theme: Theme) {
    this.output = new BoxRenderable(renderer, {
      id: 'scrim',
      width: '100%',
      height: '100%',
      position: 'absolute',
      left: 0,
      top: 0,
      backgroundColor: scrimPaint(theme),
      // Above every pane and the lists that rise out of them (5), below the
      // chat modal (20), the command overlay (25), and the theme picker (30):
      // one scrim covers the background whichever of them are open, and two
      // stacked modals never dim each other.
      zIndex: 15,
      visible: false,
    });
  }

  applyTheme(theme: Theme): void {
    this.output.backgroundColor = scrimPaint(theme);
  }

  render(modalOpen: boolean): void {
    this.output.visible = modalOpen;
  }
}
