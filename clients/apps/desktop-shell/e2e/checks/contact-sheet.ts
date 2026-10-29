/**
 * The contact sheet: every captured screen of the walk as one picture, a thumbnail and its name
 * for each, so a person (or a reviewer) sees the whole matrix at a glance and a pull request can
 * attach it. The sheet is a page of the app windows' own screenshots, never the desktop.
 */

export interface SheetEntry {
  /** The screen's name: fixture, state, level, sidebar and width. */
  readonly name: string;
  /** The screenshot's path, relative to the sheet. */
  readonly file: string;
}

const escapeHtml = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/** The sheet's page: `columns` thumbnails across, each captioned with its screen's name. */
export function contactSheetHtml(entries: readonly SheetEntry[], columns: number): string {
  const cells = entries
    .map(
      entry =>
        `<figure><img src="${escapeHtml(entry.file)}" alt=""><figcaption>${escapeHtml(entry.name)}</figcaption></figure>`,
    )
    .join('\n');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
body { margin: 0; padding: 12px; background: #fff; color: #111; font: 12px/16px system-ui, sans-serif; }
main { display: grid; grid-template-columns: repeat(${columns}, minmax(0, 1fr)); gap: 12px; }
figure { margin: 0; }
img { width: 100%; display: block; border: 1px solid #ccc; }
figcaption { padding-top: 4px; word-break: break-all; }
</style></head><body><main>
${cells}
</main></body></html>`;
}
