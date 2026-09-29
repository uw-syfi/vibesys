/** Hooks both windows share: the sidebar's listing and the New run and palette shortcuts. */
import {useEffect, useState} from 'react';
import {EMPTY_LISTING, type HomeApi, type Listing, loadListing} from './home.js';

/** The run list refreshes this often while the page is visible, so run statuses never go stale. */
const REFRESH_MS = 5_000;

export function useHome(home: HomeApi): Listing {
  const [listing, setListing] = useState(EMPTY_LISTING);
  useEffect(() => {
    let live = true;
    let ticket = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let last = EMPTY_LISTING;
    const load = () => {
      clearTimeout(timer);
      const mine = ++ticket;
      void loadListing(home, last).then(next => {
        if (!live || mine !== ticket) return;
        last = next;
        setListing(next);
        if (document.visibilityState === 'visible') timer = setTimeout(load, REFRESH_MS);
      });
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') load();
      else {
        clearTimeout(timer);
        ticket += 1;
      }
    };
    load();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      live = false;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [home]);
  return listing;
}

/**
 * ⌘N (Ctrl+N elsewhere) opens New run. Browsers may keep ⌘N for a new window; the Electron shell
 * delivers it, and the sidebar's New run row is always there.
 */
export function useNewRunShortcut(href: string | null): void {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (href === null || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'n') {
        return;
      }
      event.preventDefault();
      window.location.assign(href);
    };
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, [href]);
}

/** ⌘K (Ctrl+K elsewhere) opens the palette; an open dialog (the palette, a confirmation) keeps the keyboard. */
export function usePaletteShortcut(open: () => void): void {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return;
      event.preventDefault();
      if (document.querySelector('dialog[open], [role="alertdialog"]') !== null) return;
      open();
    };
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, [open]);
}
