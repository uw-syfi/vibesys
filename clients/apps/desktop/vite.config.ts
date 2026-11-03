import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// The page every window shows: `index.html` and the renderer entry `src/ui/main.tsx`, built to
// `dist/renderer` beside the main process's `dist/main`. The window loads it from `file:`, so asset
// URLs are relative.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {conditions: ['@vibesys/source', ...defaultClientConditions]},
  build: {emptyOutDir: true, outDir: 'dist/renderer', assetsDir: 'assets'},
});
