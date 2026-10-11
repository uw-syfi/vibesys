import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';

// The UI bundle the desktop app ships (`clients/desktop` copies `.desktop-dist` into its build).
// Relative asset URLs, because the app serves it from its own `app://` origin, not a server root.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
  build: {
    emptyOutDir: true,
    outDir: '.desktop-dist',
    rollupOptions: {
      input: fileURLToPath(new URL('./desktop.html', import.meta.url)),
    },
  },
});
