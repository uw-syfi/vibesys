/**
 * What the operating system asks for, light or dark: now, and each time the person changes it. The
 * window's own implementation reads `prefers-color-scheme`, so it lives with the browser layers in
 * `ui/shell/browser-layers.ts`; a test gives its own.
 */

import type {ThemeMode} from '@vibesys/ui/tokens';
import * as Context from 'effect/Context';
import type * as Effect from 'effect/Effect';
import type * as Stream from 'effect/Stream';

export class SystemAppearance extends Context.Service<
  SystemAppearance,
  {
    readonly current: Effect.Effect<ThemeMode>;
    /** Each later change, as it happens; ending the stream stops listening. */
    readonly changes: Stream.Stream<ThemeMode>;
  }
>()('app/SystemAppearance') {}
