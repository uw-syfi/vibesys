import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// `vite build` builds the page every window shows: `index.html` and the renderer entry
// `src/ui/main.tsx`, into `dist/renderer` beside the main process's `dist/main`. The window loads
// it from `file:`, so asset URLs are relative. `--mode fixture` builds the same screens over a
// fake daemon (`fixture.html`) into `dist/fixture`, to look at and drive in a browser.
export default defineConfig(({mode}) => {
  const fixture = mode === 'fixture';
  return {
    plugins: [react()],
    base: './',
    resolve: {conditions: ['@vibesys/source', ...defaultClientConditions]},
    build: {
      emptyOutDir: true,
      outDir: fixture ? 'dist/fixture' : 'dist/renderer',
      assetsDir: 'assets',
      rollupOptions: {input: fixture ? 'fixture.html' : 'index.html'},
    },
  };
});
