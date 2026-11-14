/**
 * Where the keyboard focus goes when the window changes place. A screen is drawn anew when the
 * person opens another, so the focus the old one held is lost and falls to the document. Opening a
 * screen lands the focus on its heading, so a reader hears what changed; going back lands it on the
 * control that opened the screen they left, found again by what it is called, so a keyboard user
 * is where they were. Pages call back as they are drawn (`pageArrival`), so no effect runs here.
 */
import {pageArrival} from '@vibesys/ui';
import {type JSX, type ReactNode, useMemo, useRef} from 'react';
import type {Navigation, NavigationAction} from '../../state/index.js';

const Arrival = pageArrival.Provider;

/** Where the window is, as one word: the same word for the same screen, however often it is drawn. */
export function placeOf(navigation: Navigation): string {
  if (navigation.machines.screen === 'add') return `add-machine/${navigation.machines.origin}`;
  if (navigation.destination !== 'runs') return navigation.destination;
  const {runs} = navigation;
  if (runs.screen !== 'run') return runs.screen;
  return `${runs.host}/${runs.runId}/${runs.view}/${runs.agentId ?? ''}`;
}

/** What names an element to a person: its label, or the text it is labelled by, or its own text. */
function nameOf(element: Element): string {
  const label = element.getAttribute('aria-label');
  if (label !== null && label !== '') return label;
  const by = element.getAttribute('aria-labelledby');
  const named = by === null ? null : element.ownerDocument.getElementById(by);
  return (named ?? element).textContent?.trim() ?? '';
}

/** An element's role and name: enough to find it again in a screen drawn anew. */
const signatureOf = (element: Element): string =>
  `${element.getAttribute('role') ?? element.tagName}:${nameOf(element)}`;

const FOCUSABLE =
  'button, a[href], input, textarea, select, [role="option"], [role="tab"], [role="treeitem"]';

/** Puts the focus where a person arriving on `page` should find it: their opener, else the heading. */
function arrive(page: HTMLElement, opener: string | undefined): void {
  const opened =
    opener === undefined
      ? undefined
      : ([...page.querySelectorAll<HTMLElement>(FOCUSABLE)].find(
          element => signatureOf(element) === opener,
        ) ?? page.querySelector<HTMLElement>('[role="option"][aria-selected="true"]'));
  if (opened !== undefined && opened !== null) {
    opened.focus();
    return;
  }
  page.querySelector<HTMLElement>('h1')?.focus({preventScroll: true});
}

/** The moves that open a page of a run, which the person comes back from. */
const opensFromAControl = (action: NavigationAction): boolean =>
  action.type === 'open-run' ||
  action.type === 'show' ||
  action.type === 'pick-agent' ||
  action.type === 'open-add-machine';

/** The moves of the rail, which start the window's trail afresh. */
const startsAfresh = (action: NavigationAction): boolean =>
  action.type === 'open-runs' ||
  action.type === 'open-cost' ||
  action.type === 'open-machines' ||
  action.type === 'open-settings';

/**
 * Moves the focus as the window changes `place`: the page that arrives takes it on its heading, or,
 * when the person is coming back to a place they opened something from, on what they opened it with.
 * `children` gets the function to call with each intent before it is sent, which keeps the control
 * that has the focus as the opener of what the intent opens.
 */
export function FocusMemory(props: {
  readonly place: string;
  readonly children: (note: (action: NavigationAction) => void) => ReactNode;
}): JSX.Element {
  const {place} = props;
  const openers = useRef(new Map<string, string>());
  // One function for as long as the place is the same, so a page calls it once as it is drawn.
  const arrival = useMemo(
    () => (page: HTMLElement) => arrive(page, openers.current.get(place)),
    [place],
  );
  const note = (action: NavigationAction): void => {
    if (startsAfresh(action)) openers.current.clear();
    const held = document.activeElement;
    if (opensFromAControl(action) && held !== null && held !== document.body) {
      openers.current.set(place, signatureOf(held));
    }
  };
  return <Arrival value={arrival}>{props.children(note)}</Arrival>;
}
