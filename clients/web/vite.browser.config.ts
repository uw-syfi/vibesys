import {fileURLToPath} from 'node:url';
import {defineConfig} from 'vite';

export default defineConfig({
  resolve: {
    alias: [
      {
        find: '@vibesys/backend-client/websocket',
        replacement: fileURLToPath(new URL('../backend-client/src/websocket.ts', import.meta.url)),
      },
      {
        find: '@vibesys/backend-client',
        replacement: fileURLToPath(new URL('../backend-client/src/index.ts', import.meta.url)),
      },
    ],
  },
  build: {
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL('./src/browser-entry.ts', import.meta.url)),
      formats: ['es'],
      fileName: 'browser-entry',
    },
    outDir: '.browser-dist',
  },
});
