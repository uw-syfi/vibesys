/**
 * The widths a person gave the Run screen's panes, kept in the viewer's browser storage, and the
 * screen's `PaneSizes` over them for as long as it shows.
 */
import {useState} from 'react';
import {PANE_ROLES, type PaneSizes, type PaneWidths} from '../../state/index.js';

export const PANE_WIDTHS_KEY = 'vibesys.run-page.pane-widths';

/** Where the widths are kept between runs of the app; none keeps them while the screen shows. */
export interface PaneWidthsMemory {
  read(): PaneWidths;
  write(widths: PaneWidths): void;
}

/** The widths in `text`: each role's positive number of pixels; anything else is no width. */
function parseWidths(text: string | null): PaneWidths {
  if (text === null) return {};
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
    const record = value as Record<string, unknown>;
    return Object.fromEntries(
      PANE_ROLES.flatMap(role => {
        const width = record[role];
        return typeof width === 'number' && Number.isFinite(width) && width > 0
          ? [[role, width]]
          : [];
      }),
    );
  } catch {
    return {};
  }
}

/**
 * The memory over browser storage. Storage can be missing, blocked or full, so every access is
 * guarded: `storage` may throw or return null, and then nothing is kept.
 */
export function browserPaneWidths(storage: () => Storage | null): PaneWidthsMemory {
  return {
    read() {
      try {
        return parseWidths(storage()?.getItem(PANE_WIDTHS_KEY) ?? null);
      } catch {
        return {};
      }
    },
    write(widths) {
      try {
        storage()?.setItem(PANE_WIDTHS_KEY, JSON.stringify(widths));
      } catch {
        // The widths last while the screen shows.
      }
    },
  };
}

/** The screen's pane widths: read from `memory` once, and each change written back to it. */
export function usePaneWidths(memory: PaneWidthsMemory | undefined): PaneSizes {
  const [widths, setWidths] = useState<PaneWidths>(() => memory?.read() ?? {});
  return {
    widths,
    change(role, width) {
      const next = {...widths, [role]: width};
      setWidths(next);
      memory?.write(next);
    },
  };
}
