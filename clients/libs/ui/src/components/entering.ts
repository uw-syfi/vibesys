import {useState} from 'react';

interface Seen {
  readonly key: string;
  readonly ids: ReadonlySet<string>;
  readonly entering: ReadonlySet<string>;
}

const NONE: ReadonlySet<string> = new Set();

/**
 * The ids that are new since the list last changed: rows that arrived while the list was on screen,
 * to draw them in with a short motion (`data-entering`). The first list drawn has none, and a row
 * that only scrolled into view is not new. With `at-end` only the rows after the last one that was
 * already there are new (a conversation grows at its end; older history arrives before it). Derived during render from the previous ids, so a
 * repeated render (strict mode, a re-render) gives the same answer.
 */
export function useEntering(
  ids: readonly string[],
  where: 'anywhere' | 'at-end' = 'anywhere',
): ReadonlySet<string> {
  const key = ids.join('\u0000');
  const [seen, setSeen] = useState<Seen | null>(null);
  if (seen === null) {
    setSeen({key, ids: new Set(ids), entering: NONE});
    return NONE;
  }
  if (seen.key === key) return seen.entering;
  // Rows that arrived before the ones already there (older history) are not drawn in.
  const lastKnown = ids.reduce((last, id, index) => (seen.ids.has(id) ? index : last), -1);
  const entering = new Set(
    ids.filter((id, index) => !seen.ids.has(id) && (where === 'anywhere' || index > lastKnown)),
  );
  setSeen({key, ids: new Set(ids), entering});
  return entering;
}
