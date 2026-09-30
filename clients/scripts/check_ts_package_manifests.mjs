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
  }
  return errors;
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
