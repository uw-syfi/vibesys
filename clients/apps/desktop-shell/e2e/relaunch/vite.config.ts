import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

/** A test-only composition; the shipping shell entry remains unchanged. */
export default defineConfig({
  root: fileURLToPath(new URL('./', import.meta.url)),
  plugins: [react()],
  base: './',
  resolve: {conditions: ['@vibesys/source', ...defaultClientConditions]},
  build: {
    emptyOutDir: false,
    outDir: fileURLToPath(new URL('../../dist/relaunch/', import.meta.url)),
    rollupOptions: {input: fileURLToPath(new URL('./fixture.html', import.meta.url))},
  },
});
