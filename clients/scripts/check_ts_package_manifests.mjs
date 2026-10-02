#!/usr/bin/env node

import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {workspaceLayout} from './workspace_layout.mjs';

// The dependency direction each package may declare. The package set itself is derived
// (`workspace_layout.mjs`), so this holds policy only: a package on disk without an entry here
// fails the check by name instead of being skipped.
const POLICY = {
  '@vibesys/backend-client': {
    runtimeWorkspaceDependencies: [],
    forbiddenDependencyPrefixes: [],
  },
  '@vibesys/core-state': {
    runtimeWorkspaceDependencies: ['@vibesys/backend-client'],
    forbiddenDependencyPrefixes: ['@opentui/'],
  },
  '@vibesys/tui': {
    runtimeWorkspaceDependencies: ['@vibesys/backend-client', '@vibesys/core-state'],
    forbiddenDependencyPrefixes: [],
  },
  '@vibesys/web': {
    runtimeWorkspaceDependencies: ['@vibesys/backend-client', '@vibesys/core-state'],
    forbiddenDependencyPrefixes: ['@vibesys/tui', '@opentui/'],
  },
  '@vibesys/desktop': {
    directory: 'desktop',
    runtimeWorkspaceDependencies: [],
    forbiddenDependencyPrefixes: [],
  },
};

const DEPENDENCY_SECTIONS = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
];

export function manifestErrors(root) {
  const errors = [];
  const {packages} = workspaceLayout(root);
  const workspaceNames = new Set(packages.map(({name}) => name));
  for (const {name, directory, manifest} of packages) {
    const relativePath = join(directory, 'package.json');
    const policy = POLICY[name];
    if (policy === undefined) {
      errors.push(
        `${relativePath}: ${name} has no dependency policy in ` +
          'scripts/check_ts_package_manifests.mjs',
      );
      continue;
    }
    errors.push(...packageErrors(relativePath, name, policy, manifest, workspaceNames));
    errors.push(...buildOrderErrors(relativePath, name, policy, manifest));
  }
  return errors;
}

/**
 * Whether a package's tests build the workspace dependencies they import.
 *
 * `pnpm -r run test` runs the packages in topological order but runs only `test`, so nothing in
 * that order writes the `dist` a workspace import resolves to. pnpm links every declared workspace
 * dependency into `<package>/node_modules/@vibesys/*`, and that link resolves through the
 * dependency's `exports` to its `dist`, so the import reads build output unless every entry point
 * is redirected to source by a tsconfig `paths` entry. The hook is required of a package with a
 * workspace dependency whether or not it has that redirection today: the redirection is per
 * specifier, so adding a subpath import or dropping an alias silently puts the suite back on
 * whichever `dist` the last build in the checkout left behind. `@vibesys/web` declared no
 * `pretest` at all (#1039). Its argument is derivable from the package name, so this checks the
 * derived value rather than accepting any script.
 */
function buildOrderErrors(relativePath, name, policy, manifest) {
  if (policy.runtimeWorkspaceDependencies.length === 0) return [];
  const expected = `pnpm --filter ${name}^... build`;
  if (manifest.scripts?.pretest === expected) return [];
  return [
    `${relativePath}: ${name} must declare "pretest": "${expected}", because ` +
      '`pnpm -r test` runs the packages in order but builds none of them',
  ];
}

// biome-ignore lint/complexity/noExcessiveCognitiveComplexity: pre-existing; tracked: #288
function packageErrors(relativePath, name, policy, manifest, workspaceNames) {
  const errors = [];
  const allowed = new Set(policy.runtimeWorkspaceDependencies);
  const declaredWorkspaceDependencies = new Set();
  const runtimeWorkspaceDependencies = new Set(
    Object.keys(manifest.dependencies ?? {}).filter(dependency => workspaceNames.has(dependency)),
  );
  for (const section of DEPENDENCY_SECTIONS) {
    const dependencies = manifest[section] ?? {};
    for (const dependency of Object.keys(dependencies)) {
      if (workspaceNames.has(dependency)) declaredWorkspaceDependencies.add(dependency);
      if (policy.forbiddenDependencyPrefixes.some(prefix => dependency.startsWith(prefix))) {
        errors.push(`${relativePath}: ${name} must not depend on ${dependency}`);
      }
    }
  }
  for (const dependency of declaredWorkspaceDependencies) {
    if (!allowed.has(dependency)) {
      errors.push(`${relativePath}: ${name} must not depend on ${dependency}`);
    }
  }
  for (const dependency of allowed) {
    if (!runtimeWorkspaceDependencies.has(dependency)) {
      errors.push(`${relativePath}: ${name} must declare ${dependency} in dependencies`);
    }
  }
  return errors;
}

function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const errors = manifestErrors(root);
  if (errors.length === 0) {
    console.log('TypeScript package manifests respect dependency direction.');
    return 0;
  }
  console.error('TypeScript package manifest violations:');
  for (const error of errors) console.error(`- ${error}`);
  return 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
