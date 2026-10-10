import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';
import {workspaceSourceAliases} from './workspace-source-aliases.js';

// The UI bundle the desktop app ships (`clients/desktop` copies `.desktop-dist` into its build).
// Relative asset URLs, because the app serves it from its own `app://` origin, not a server root.
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: workspaceSourceAliases(),
  },
  build: {
    emptyOutDir: true,
    outDir: '.desktop-dist',
    rollupOptions: {
      input: fileURLToPath(new URL('./desktop.html', import.meta.url)),
    },
  },
});
