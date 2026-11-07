/** The page the theme is shown on: a role, so a test reads what was shown without a document. */

import type {ThemeName} from '@vibesys/ui/tokens';
import * as Context from 'effect/Context';
import type * as Effect from 'effect/Effect';

export class ThemeDocument extends Context.Service<
  ThemeDocument,
  {
    /** Shows the page in `theme`. Showing the theme it shows already changes nothing. */
    readonly show: (theme: ThemeName) => Effect.Effect<void>;
  }
>()('app/ThemeDocument') {}
