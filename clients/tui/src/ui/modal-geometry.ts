/** Rows a modal may occupy in root coordinates. */
export interface ModalViewport {
  top: number;
  height: number;
}

/** A modal's vertical rectangle in root coordinates. */
export interface ModalRows {
  top: number;
  height: number;
}

/**
 * Fits a modal's preferred rows inside the viewport the root layout owns.
 *
 * Shares stay with each presentation because different modals intentionally
 * use different proportions. This function owns the common invariant: an
 * absolute modal starts at or below the viewport and cannot extend past it.
 * Bottom rows are preserved when the viewport can afford them, while the
 * modal's readable minimum wins on a tighter viewport.
 */
export function fitModalRows(
  viewport: ModalViewport,
  preferredTop: number,
  preferredHeight: number,
  minimumHeight: number,
  bottomRows = 0,
): ModalRows {
  const offset = Math.min(viewport.height, Math.max(0, preferredTop));
  const remaining = Math.max(0, viewport.height - offset);
  const heightWithoutBottomRows = Math.max(0, remaining - bottomRows);
  const readableHeight = Math.max(
    minimumHeight,
    Math.min(preferredHeight, heightWithoutBottomRows),
  );
  return {
    top: viewport.top + offset,
    height: Math.min(remaining, readableHeight),
  };
}
