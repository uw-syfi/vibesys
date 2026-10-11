/**
 * The welcome view's bundle entry (`welcome.html`, the package's `dom` entry): it loads the view's
 * composition root. Everything under `src/renderer/` runs in a renderer (the welcome view) or in a
 * sandboxed preload beside one, never in the main process.
 */
import './welcome/main.js';
