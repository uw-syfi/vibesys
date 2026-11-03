/**
 * Sandboxed preload (`preload` runtime): the typed bridge between the page and the main process.
 * Placeholder until the first request type exists. A sandboxed preload loads only Electron at run
 * time (`preload-sandbox`), so it never imports `effect`; it carries types only.
 */
export {};
