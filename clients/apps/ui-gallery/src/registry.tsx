import {BASIC_ENTRIES} from './entries-basic.js';
import {RICH_ENTRIES} from './entries-rich.js';
import {THREAD_ENTRIES} from './entries-thread.js';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

export type {ComponentName, GalleryEntry} from './gallery-entry.js';

/** Every public component, keyed by its exported name: a missing one is a type error. */
export const REGISTRY: Readonly<Record<ComponentName, GalleryEntry>> = {
  ...BASIC_ENTRIES,
  ...RICH_ENTRIES,
  ...THREAD_ENTRIES,
};
