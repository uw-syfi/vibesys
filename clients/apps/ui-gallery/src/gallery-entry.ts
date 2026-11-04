import type * as ui from '@vibesys/ui';
import type {ReactNode} from 'react';

/** The names of the components `@vibesys/ui`'s `.` entry exports: its capitalized functions. */
export type ComponentName = {
  [K in keyof typeof ui]: (typeof ui)[K] extends (...args: never[]) => unknown
    ? K extends Capitalize<K>
      ? K
      : never
    : never;
}[keyof typeof ui];

/** One component's gallery entry: every state it has, in fixed content. */
export interface GalleryEntry {
  readonly render: () => ReactNode;
}
