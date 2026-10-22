#!/usr/bin/env node

/** Client package scripts must use the runners that own collection and hang guards. */
import {readFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {workspaceLayout} from './workspace_layout.mjs';

/** Pure manifest policy, shared by the workspace gate and synthetic manifest tests. */
export function testRunnerErrors(path, manifest) {
  const errors = [];
  for (const [name, script] of Object.entries(manifest.scripts ?? {})) {
    for (const command of script.split(/&&|\|\||[;|]/)) {
      const words = command.trim().split(/\s+/);
      const raw = /(?:^|\s)(?:bun\s+test|node\s+[^;&|]*--test(?:\s|=|$))/.test(command);
      const runner = words.findIndex(word =>
        /(?:^|\/)run_(?:bun_tests|node_tests|node_suite)\.mjs$/.test(word),
      );
      const overrides = runner >= 0 && words.slice(runner + 1).some(word => word.startsWith('-'));
      if (raw || overrides) {
        errors.push(
          `test-runner-policy: ${path}: scripts.${name}: use run_bun_tests.mjs, ` +
            'run_node_tests.mjs or run_node_suite.mjs without runner flags; ' +
            'the shared configuration owns the per-test hang guard',
        );
      }
    }
  }
  return errors;
}

/** Runner changes must invalidate every package's cached test result. */
export function testRunnerCacheErrors(turbo) {
  return [
    'tools/scripts/run_bun_tests.mjs',
    'tools/scripts/run_node_tests.mjs',
    'tools/scripts/run_node_suite.mjs',
    'tools/scripts/test_runner_config.mjs',
  ]
    .filter(path => !turbo.globalDependencies?.includes(path))
    .map(
      path =>
        `test-runner-policy: turbo.json: add ${path} to globalDependencies so cached tests cannot bypass a changed hang guard`,
    );
}

export function workspaceTestRunnerErrors(root) {
  const manifests = [
    {directory: '.', manifest: JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'))},
    ...workspaceLayout(root).packages,
  ];
  return [
    ...manifests.flatMap(({directory, manifest}) =>
      testRunnerErrors(join(directory, 'package.json'), manifest),
    ),
    ...testRunnerCacheErrors(JSON.parse(readFileSync(join(root, 'turbo.json'), 'utf8'))),
  ];
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
  const errors = workspaceTestRunnerErrors(root);
  for (const error of errors) console.error(error);
  process.exitCode = errors.length > 0 ? 1 : 0;
}
