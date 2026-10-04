import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import type {Alias} from 'vite';

type AliasPaths = Record<string, string>;

/**
 * Resolve the dedicated workspace source paths into Vite aliases.
 *
 * This strict JSON file is deliberately separate from `tsconfig.json`: TypeScript
 * configuration allows JSONC comments, whereas Vite must read its aliases with
 * Node before TypeScript parses that configuration. The architecture gate checks
 * both declarations against the workspace packages' public exports.
 */
export function workspaceSourceAliases(): Alias[] {
  const paths = JSON.parse(
    readFileSync(new URL('./workspace-source-aliases.json', import.meta.url), 'utf8'),
  ) as AliasPaths;

  // Vite applies the first matching alias, so a package's subpaths must precede
  // its root entry. `@vibesys/backend-client` would otherwise capture
  // `@vibesys/backend-client/websocket` and append the suffix to index.ts.
  return Object.entries(paths)
    .sort(([left], [right]) => right.length - left.length)
    .map(([find, replacement]) => {
      return {
        find,
        replacement: fileURLToPath(new URL(replacement, import.meta.url)),
      };
    });
}
