#!/usr/bin/env node

import {readFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const PACKAGES = {
  '@vibesys/backend-client': {
    directory: 'backend-client',
    runtimeWorkspaceDependencies: [],
    forbiddenDependencyPrefixes: [],
  },
  '@vibesys/core-state': {
    directory: 'core-state',
    runtimeWorkspaceDependencies: ['@vibesys/backend-client'],
    forbiddenDependencyPrefixes: ['@opentui/'],
  },
  '@vibesys/tui': {
    directory: 'tui',
    runtimeWorkspaceDependencies: ['@vibesys/backend-client', '@vibesys/core-state'],
    forbiddenDependencyPrefixes: [],
  },
};

const DEPENDENCY_SECTIONS = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
];

// biome-ignore lint/complexity/noExcessiveCognitiveComplexity: pre-existing; tracked: #288
export async function manifestErrors(root) {
  const errors = [];
  const workspaceNames = new Set(Object.keys(PACKAGES));
  for (const [expectedName, policy] of Object.entries(PACKAGES)) {
    const relativePath = join('clients', policy.directory, 'package.json');
    let manifest;
    try {
      manifest = JSON.parse(await readFile(join(root, relativePath), 'utf8'));
    } catch (error) {
      errors.push(`${relativePath}: cannot read package manifest: ${error.message}`);
      continue;
    }
    if (manifest.name !== expectedName) {
      errors.push(`${relativePath}: expected package name ${expectedName}`);
    }
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
          errors.push(`${relativePath}: ${expectedName} must not depend on ${dependency}`);
        }
      }
    }
    for (const dependency of declaredWorkspaceDependencies) {
      if (!allowed.has(dependency)) {
        errors.push(`${relativePath}: ${expectedName} must not depend on ${dependency}`);
      }
    }
    for (const dependency of allowed) {
      if (!runtimeWorkspaceDependencies.has(dependency)) {
        errors.push(`${relativePath}: ${expectedName} must declare ${dependency} in dependencies`);
      }
    }
  }
  return errors;
}

async function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const errors = await manifestErrors(root);
  if (errors.length === 0) {
    console.log('TypeScript package manifests respect dependency direction.');
    return 0;
  }
  console.error('TypeScript package manifest violations:');
  for (const error of errors) console.error(`- ${error}`);
  return 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
