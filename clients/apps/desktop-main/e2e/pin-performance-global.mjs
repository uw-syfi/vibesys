/**
 * Pin the Electron main process's `globalThis.performance` before any page loads, for the E2E
 * fixture apps. Import it first, for its side effect.
 *
 * Node defines `performance` as a lazy global. Its getter loads `perf_hooks` on first read and
 * caches that module's exports object; read while `perf_hooks` is still loading, it caches a
 * partial exports object that `perf_hooks` then replaces, so `performance` stays `undefined` for
 * the rest of the process. Playwright's `ElectronApplication.evaluate` runs its utility script
 * through the inspector, at a V8 interrupt in the middle of whatever JavaScript is running, and
 * that script reads `performance`. The first `app://` request loads `perf_hooks`: Electron's
 * `protocol.handle` builds a `Headers`, which loads undici, which loads `perf_hooks`. An evaluate
 * landing inside that load leaves undici unable to initialize, and every `app://` load then fails
 * with `ERR_UNEXPECTED` (-9) (the flake in #2972).
 *
 * Assigning the global goes through its setter, which pins the value for good: from here on no
 * read reloads `perf_hooks` and no evaluate, at any point, can see a partial module. The
 * production app runs no inspector, so nothing interrupts its first load.
 */
import {performance} from 'node:perf_hooks';

globalThis.performance = performance;
