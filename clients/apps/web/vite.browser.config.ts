import {fileURLToPath} from 'node:url';
import {defaultClientConditions, defineConfig} from 'vite';

export default defineConfig({
  resolve: {
    // Workspace packages resolve to their TypeScript source, so no dependency needs a build.
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
  build: {
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL('./src/platform/backend.ts', import.meta.url)),
      formats: ['es'],
      fileName: 'browser-entry',
    },
    outDir: '.browser-dist',
  },
});
