import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// The welcome view's bundle (`welcome.html`, `src/main.tsx`): the desktop app's own trusted page,
// built apart from the run view (`@vibesys/app-web`'s desktop bundle), so the run page's bundle
// names no host, command, or path. `clients/apps/desktop-main/scripts/copy-ui.mjs` copies it beside
// the run view in the app's `dist/ui`, served from `app://vibesys`, so asset URLs are relative and
// the assets get their own directory, where the two bundles never collide.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
  build: {
    emptyOutDir: true,
    outDir: 'dist',
    assetsDir: 'welcome-assets',
    rollupOptions: {
      input: fileURLToPath(new URL('./welcome.html', import.meta.url)),
    },
  },
});
