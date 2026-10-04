import {fileURLToPath} from 'node:url';
import {defineConfig} from 'vite';
import {workspaceSourceAliases} from './workspace-source-aliases.js';

export default defineConfig({
  resolve: {
    alias: workspaceSourceAliases(),
  },
  build: {
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL('./src/browser-entry.ts', import.meta.url)),
      formats: ['es'],
      fileName: 'browser-entry',
    },
    outDir: '.browser-dist',
  },
});
