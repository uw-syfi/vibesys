import type {JSX, ReactNode} from 'react';

/**
 * The window's layout: a column at its start and the page beside it, both from the top of the
 * window to the bottom, so the page begins at the window's top edge with no strip above it. The
 * window's own controls are facts the desktop app sets on the root element, which the frame keeps clear:
 * the macOS traffic lights sit in the column's top-left corner (`--chrome-start-width` by
 * `--chrome-start-height`), so the column is at least that wide and what it holds starts below the
 * corner; Windows and Linux draw their controls at the top right, so the page starts below the
 * strip they cover (`--chrome-end-height`). Full screen hides the controls and sets all three to
 * zero. The column and a band across the top of the page drag the window, and every control inside
 * them stays a control. `start` is the column, which holds only the controls when left out.
 */
export function WindowFrame(props: {
  readonly start?: ReactNode;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <div className="vs-window-frame">
      {props.start ?? <div className="vs-window-start" />}
      <div className="vs-app-main">
        <div className="vs-window-drag" />
        <div className="vs-app-content">{props.children}</div>
      </div>
    </div>
  );
}
