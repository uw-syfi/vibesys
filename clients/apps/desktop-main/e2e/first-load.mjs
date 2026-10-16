/**
 * Waiting on an Electron page's first load by its own events, for the E2E fixture apps.
 *
 * A spec that polls for a page's URL under a fixed deadline depends on how fast Electron commits
 * the first navigation, which on a cold start (a freshly downloaded binary, a busy runner) can take
 * longer than any such deadline (PR #2885's flake). These promises settle on the load events
 * instead, so the spec's only bound is a hang guard.
 */

/**
 * Settles when `contents` finishes or fails its first main-frame load, or is destroyed first.
 *
 * Call it in the same task that starts the load (or in `web-contents-created`): Electron delivers
 * load events asynchronously, so none can fire before the listeners below are attached. A failed
 * load rejects with its reason instead of leaving the spec to time out on missing state.
 */
export function firstLoad(contents) {
  const load = new Promise((resolve, reject) => {
    contents.once('did-finish-load', () => resolve());
    contents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
      if (isMainFrame) reject(new Error(`${url} failed to load: ${code} ${description}`));
    });
    contents.once('destroyed', () => reject(new Error('the page closed before it loaded')));
  });
  // Observed here so a page nobody awaits never surfaces as an unhandled rejection.
  load.catch(() => {});
  return load;
}
