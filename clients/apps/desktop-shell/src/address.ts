/**
 * The window's address and keys as the Run screen keeps its zoom in them: the zoom is the
 * address's fragment (a run window's query names its run, so the two never meet), each zoom adds a
 * history entry, Back and Forward restore one, and Escape outside a text field goes up a level.
 */
import type {RunPageAddress} from '@vibesys/feature-run-page/renderer';

/** Whether a key press belongs to a text field (its Escape clears or leaves the field). */
function inTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target.isContentEditable
  );
}

export function windowAddress(window: Window): RunPageAddress {
  return {
    fragment: () => window.location.hash.replace(/^#/, ''),
    push: fragment => {
      const url = `${window.location.pathname}${window.location.search}${fragment === '' ? '' : `#${fragment}`}`;
      window.history.pushState(null, '', url);
    },
    onRestore: listener => {
      const restore = (): void => listener(window.location.hash.replace(/^#/, ''));
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
