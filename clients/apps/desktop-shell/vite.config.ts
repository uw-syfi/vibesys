import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// The app shell's bundle (`shell.html`, `src/main.tsx`), shown in the window's trusted surface
// under `VIBESYS_DESKTOP_SHELL=next`. `clients/apps/desktop-main/scripts/copy-ui.mjs` copies it
// beside the other pages in the app's `dist/ui`, served from `app://vibesys`, so asset URLs are
// relative and the assets get their own directory.
const appManifest = new URL('../desktop-main/package.json', import.meta.url);
const {version} = JSON.parse(readFileSync(appManifest, 'utf8')) as {version: string};

export default defineConfig({
  plugins: [react()],
  base: './',
  define: {'import.meta.env.VITE_APP_VERSION': JSON.stringify(version)},
  resolve: {
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
  build: {
    emptyOutDir: true,
    outDir: 'dist',
    assetsDir: 'shell-assets',
    rollupOptions: {input: fileURLToPath(new URL('./shell.html', import.meta.url))},
  },
});
