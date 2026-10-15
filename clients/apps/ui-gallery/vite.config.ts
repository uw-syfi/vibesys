import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// The component gallery: a development page, never shipped. The fonts come from
// `@vibesys/ui/fonts.css`, so the page fetches nothing from the network.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
  build: {emptyOutDir: true, outDir: 'dist'},
});
