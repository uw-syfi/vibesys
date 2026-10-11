import {createHash} from 'node:crypto';
import {readdir, readFile, stat, writeFile} from 'node:fs/promises';
import {join, relative, resolve, sep} from 'node:path';
import type {Plugin, ResolvedConfig} from 'vite';

export const WEB_BUILD_MANIFEST = '.vibesys-web-build.json';

interface BuildManifestOptions {
  workspaceRoot: string;
  buildInputs: readonly string[];
}

interface ManifestSource {
  path: string;
  files: Record<string, string>;
}

interface WebBuildManifest {
  version: 1;
  build_id: string;
  workspace: string;
  sources: ManifestSource[];
  assets: Record<string, string>;
}

type SourceManifest = Omit<WebBuildManifest, 'assets' | 'build_id'>;

/** Emit the build-input inventory that lets the gateway reject a stale bundle. */
export function webBuildManifest(options: BuildManifestOptions): Plugin {
  let config: ResolvedConfig | undefined;
  let sourceManifest: SourceManifest | undefined;
  return {
    name: 'vibesys-web-build-manifest',
    apply: 'build',
    configResolved(resolvedConfig) {
      config = resolvedConfig;
    },
    async buildStart() {
      if (config === undefined) {
        throw new Error('Vite did not resolve the web build configuration');
      }
      sourceManifest = await createManifest(resolve(config.root, config.build.outDir), options);
    },
    async writeBundle() {
      if (config === undefined || sourceManifest === undefined) {
        throw new Error('Web build manifest was not captured before bundling');
      }
      const outputDirectory = resolve(config.root, config.build.outDir);
      const assets = await snapshotFiles(outputDirectory, new Set([WEB_BUILD_MANIFEST]));
      const artifact = createHash('sha256');
      for (const [fileName, digest] of Object.entries(assets)) {
        artifact.update(fileName);
        artifact.update('\0');
        artifact.update(digest);
        artifact.update('\0');
      }
      const manifest: WebBuildManifest = {
        ...sourceManifest,
        build_id: `sha256:${artifact.digest('hex')}`,
        assets,
      };
      await writeFile(
        join(outputDirectory, WEB_BUILD_MANIFEST),
        `${JSON.stringify(manifest, undefined, 2)}\n`,
        'utf8',
      );
    },
  };
}

async function createManifest(
  outputDirectory: string,
  options: BuildManifestOptions,
): Promise<SourceManifest> {
  const workspaceRoot = resolve(options.workspaceRoot);
  const sources = await Promise.all(
    options.buildInputs.map(async (buildInput): Promise<ManifestSource> => {
      const resolvedInput = resolve(buildInput);
      const inputPath = workspacePath(workspaceRoot, resolvedInput);
      return {
        path: inputPath,
        files: await snapshotFiles(resolvedInput),
      };
    }),
  );
  sources.sort((left, right) => left.path.localeCompare(right.path));
  return {
    version: 1,
    workspace: portablePath(relative(resolve(outputDirectory), workspaceRoot)),
    sources,
  };
}

async function snapshotFiles(
  root: string,
  ignored: ReadonlySet<string> = new Set(),
): Promise<Record<string, string>> {
  const paths = await sourceFiles(root);
  const entries = await Promise.all(
    paths.map(async (path): Promise<[string, string]> => {
      const digest = createHash('sha256')
        .update(await readFile(path))
        .digest('hex');
      const relativePath = relative(root, path);
      return [relativePath === '' ? '.' : portablePath(relativePath), digest];
    }),
  );
  return Object.fromEntries(entries.filter(([path]) => !ignored.has(path)));
}

async function sourceFiles(path: string): Promise<string[]> {
  const metadata = await stat(path);
  if (metadata.isFile()) return [path];
  if (!metadata.isDirectory()) return [];

  const entries = await readdir(path, {withFileTypes: true});
  const paths: string[] = [];
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const child = resolve(path, entry.name);
    if (entry.isDirectory()) {
      paths.push(...(await sourceFiles(child)));
    } else if (entry.isFile()) {
      paths.push(child);
    }
  }
  return paths;
}

function workspacePath(workspaceRoot: string, buildInput: string): string {
  const path = relative(workspaceRoot, buildInput);
  if (path === '..' || path.startsWith(`..${sep}`) || path === '') {
    throw new Error(`Web build input must be a child of the workspace: ${buildInput}`);
  }
  return portablePath(path);
}

function portablePath(path: string): string {
  return path.split(sep).join('/');
}
