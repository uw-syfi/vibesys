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
 * that only scrolled into view is not new. Derived during render from the previous ids, so a
 * repeated render (strict mode, a re-render) gives the same answer.
 */
export function useEntering(ids: readonly string[]): ReadonlySet<string> {
  const key = ids.join('\u0000');
  const [seen, setSeen] = useState<Seen | null>(null);
  if (seen === null) {
    setSeen({key, ids: new Set(ids), entering: NONE});
    return NONE;
  }
  if (seen.key === key) return seen.entering;
  const entering = new Set(ids.filter(id => !seen.ids.has(id)));
  setSeen({key, ids: new Set(ids), entering});
  return entering;
}
