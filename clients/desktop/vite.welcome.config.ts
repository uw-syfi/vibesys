import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

// The welcome view's bundle (`welcome.html`, `src/welcome/`): the app's own trusted page, built
// apart from the run view (`@vibesys/web`'s desktop bundle), so the run page's bundle names no
// host, command, or path. `scripts/copy-ui.mjs` copies it beside the run view in `dist/ui`, served
// from `app://vibesys`; its assets get their own directory so the two bundles never collide.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    emptyOutDir: true,
    outDir: '.welcome-dist',
    assetsDir: 'welcome-assets',
    rollupOptions: {
      input: fileURLToPath(new URL('./welcome.html', import.meta.url)),
    },
  },
});
