import {fileURLToPath} from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import {defaultClientConditions, defineConfig} from 'vite';
import {webBuildInputs} from './build-inputs.js';
import {webBuildManifest} from './build-manifest.js';
import {campaignStreamPlugin} from './campaign-stream.js';

const workspaceRoot = fileURLToPath(new URL('../../../', import.meta.url));

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    campaignStreamPlugin(),
    webBuildManifest({
      workspaceRoot,
      buildInputs: webBuildInputs(),
    }),
  ],
  resolve: {
    conditions: ['@vibesys/source', ...defaultClientConditions],
  },
});
