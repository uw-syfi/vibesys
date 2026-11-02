import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// The app shell's bundle (`shell.html`, `src/main.tsx`), the page every window shows.
// `clients/apps/desktop-main/scripts/copy-ui.mjs` copies it into the app's `dist/ui`, served from
// `app://vibesys`, so asset URLs are relative.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
  build: {
    emptyOutDir: true,
    outDir: 'dist',
    assetsDir: 'shell-assets',
    rollupOptions: {
      input: [fileURLToPath(new URL('./shell.html', import.meta.url))],
    },
  },
});
