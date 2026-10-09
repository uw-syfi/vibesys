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
    forbiddenDependencyPrefixes: ['@vibesys/tui'],
  },
};

const DEPENDENCY_SECTIONS = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
];

const DEPENDENCY_BUILD_COMMANDS = ['build', 'check', 'test'];

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
 * Whether each direct package command builds the workspace dependencies it imports.
 *
 * A direct `pnpm --filter <package> <command>` has no workspace scheduler to prepare its runtime
 * dependencies. Recursive `check` and `test` runs order packages, but invoke only that command,
 * which doesn't emit the dependency `dist` their imports resolve through. The hook is required
 * whether or not every entry point is redirected to source today: aliases are per specifier, so a
 * new subpath import can silently return to build output. The hook's argument is derivable from the
 * package name, and the required command set is closed here, so the checker accepts neither a
 * partial lifecycle nor an arbitrary script that happens to start with pnpm (#1039).
 */
function buildOrderErrors(relativePath, name, policy, manifest) {
  if (policy.runtimeWorkspaceDependencies.length === 0) return [];
  const expected = `pnpm --filter ${name}^... build`;
  return DEPENDENCY_BUILD_COMMANDS.flatMap(command => {
    const hook = `pre${command}`;
    if (manifest.scripts?.[hook] === expected) return [];
    return [
      `${relativePath}: ${name} must declare "${hook}": "${expected}", because direct ` +
        `"${command}" must build its runtime workspace dependencies first`,
    ];
  });
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
