/**
 * How a viewer, an install of the app and the name it shows, is said to a host: as the metadata of
 * every call (`vibesys-viewer-id` and `vibesys-viewer-label`, both or neither), which the host
 * reads to say whose check-ins it keeps for Catch me up. The names of the two fields are the
 * host's (`orchestrator/internal/protocol`). Nothing authenticates them and they grant nothing.
 *
 * Both travel as HTTP/2 header values, which a header cannot hold outside printable ASCII, so a
 * label made from a person's name (the operating system's user name) is cut down to what a header
 * can carry. The preferences contract (`DesktopViewer`) holds a saved viewer to the same rule.
 */

/** An install of the app and the name it shows. */
export interface Viewer {
  readonly id: string;
  readonly label: string;
}

export const VIEWER_ID_KEY = 'vibesys-viewer-id';
export const VIEWER_LABEL_KEY = 'vibesys-viewer-label';

/** The longest the host takes an id or a label, in bytes (and so in characters of ASCII). */
const MOST_BYTES = 128;

/** What a label is called when nothing of a name is left for it. */
const NO_NAME = 'this install';

/**
 * `name` as a label a header can carry: a character outside printable ASCII becomes `?`, space
 * around it goes, and it is at most 128 characters; a name with nothing left is `this install`.
 */
export function labelFromName(name: string): string {
  const printable = [...name].map(char => (/^[ -~]$/.test(char) ? char : '?')).join('');
  const label = printable.slice(0, MOST_BYTES).trim();
  return label === '' ? NO_NAME : label;
}

/** The metadata that says who is calling: the same two fields on every call. */
export function viewerMetadata(viewer: Viewer): Readonly<Record<string, string>> {
  return {[VIEWER_ID_KEY]: viewer.id, [VIEWER_LABEL_KEY]: viewer.label};
}
