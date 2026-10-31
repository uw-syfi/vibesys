/**
 * The window's address, history and input as the shell keeps its location in them: the fragment
 * names the location (`fragmentOf`; a run window's query names its run, so there the fragment is
 * the zoom alone), each navigation adds a history entry, Back and Forward restore one, Escape
 * outside a text field goes up a level, and the mouse's back and forward buttons go Back and Forward.
 * Back and Forward also return each scrolled element to where it was left (`scrollKeeper`).
 */
import type {WindowAddress} from '@vibesys/feature-app-shell/renderer';
import {fragmentOf, type Screen, screenOf} from '@vibesys/feature-app-shell/state';
import type {RunPageAddress} from '@vibesys/feature-run-page/renderer';
import type {RunRef} from '@vibesys/runs';
import {scrollKeeper} from './scroll-memory.js';

/** Whether a key press belongs to a text field (its Escape clears or leaves the field). */
function inTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target.isContentEditable
  );
}

/** The address of `window`, which shows only `run` when its query names one. */
export function windowAddress(
  window: Window,
  run: RunRef | null,
): WindowAddress & Pick<RunPageAddress, 'onEscape'> {
  const fragment = (screen: Screen): string =>
    run === null ? fragmentOf(screen) : screen.kind === 'run' ? (screen.zoom ?? '') : '';
  const shown = (): Screen => {
    const named = window.location.hash.replace(/^#/, '');
    if (run === null) return screenOf(named);
    return named === '' ? {kind: 'run', run} : {kind: 'run', run, zoom: named};
  };
  const scroll = scrollKeeper(window);
  return {
    screen: shown,
    push: screen => {
      const hash = fragment(screen);
      const url = `${window.location.pathname}${window.location.search}${hash === '' ? '' : `#${hash}`}`;
      scroll.leave();
      window.history.pushState({entry: scroll.advance()}, '', url);
    },
    back: () => window.history.back(),
    onRestore: listener => {
      const restore = (): void => listener(shown());
      window.addEventListener('popstate', restore);
      return () => window.removeEventListener('popstate', restore);
    },
    onEscape: listener => {
      const press = (event: KeyboardEvent): void => {
        if (event.key !== 'Escape' || event.defaultPrevented || inTextField(event.target)) return;
        listener();
      };
      window.addEventListener('keydown', press);
      return () => window.removeEventListener('keydown', press);
    },
  };
}

const MOUSE_BACK = 3;
const MOUSE_FORWARD = 4;

/**
 * Button 3 goes Back and button 4 Forward, as a browser's do, wherever the pointer is. Taking the
 * release here keeps it from the engine's own handling, so one press moves one entry. Returns a stop.
 */
export function followMouseButtons(window: Window): () => void {
  const release = (event: MouseEvent): void => {
    if (event.defaultPrevented) return;
    if (event.button !== MOUSE_BACK && event.button !== MOUSE_FORWARD) return;
    event.preventDefault();
    if (event.button === MOUSE_BACK) window.history.back();
    else window.history.forward();
  };
  window.addEventListener('mouseup', release);
  return () => window.removeEventListener('mouseup', release);
}
