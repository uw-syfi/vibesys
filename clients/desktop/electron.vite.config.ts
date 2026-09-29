import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {defineConfig} from 'electron-vite';
import web from '../web/vite.config.ts';

const WEB_ROOT = fileURLToPath(new URL('../web', import.meta.url));

/** Sandboxed preloads must be CommonJS; main is built the same way, next to it. */
function commonJs(entry: string) {
  return {lib: {entry, formats: ['cjs' as const], fileName: () => 'index.cjs'}};
}

export default defineConfig(({command}) => ({
  main: {build: commonJs('src/main/index.ts')},
  preload: {build: commonJs('src/preload/index.ts')},
  // `electron-vite dev` serves clients/web with its own Vite config (aliases, the /api proxy)
  // and hot reload, on the origin the home server accepts through --dev-origin. The built app
  // is clients/web/dist, served by the home server, so `build` has no renderer.
  ...(command === 'serve'
    ? {
        renderer: {
          ...web,
          root: WEB_ROOT,
          server: {...web.server, host: '127.0.0.1', port: 5173, strictPort: true},
          // electron-vite validates the renderer input in serve mode too, and defaults it to
          // src/renderer/index.html under this package, not `root`.
          build: {rollupOptions: {input: join(WEB_ROOT, 'index.html')}},
        },
      }
    : {}),
}));
