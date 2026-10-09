#!/usr/bin/env node

/**
 * The architecture gate: dependency-cruiser over every source directory the workspace has.
 *
 * The scan roots are derived from `workspace_layout.mjs` instead of being listed here, because a
 * listed root silently skips a package or a directory that joins the workspace later. That is how
 * `web/e2e` stayed unscanned while `production-dependencies-are-declared`, the rule that should
 * have reported its undeclared `@playwright/test` import, was present and correct.
 */

import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {cruise} from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';
import extractTSConfig from 'dependency-cruiser/config-utl/extract-ts-config';
import {declarationErrors, workspaceLayout} from './workspace_layout.mjs';

const CONFIG_FILE = '.dependency-cruiser.mjs';

/**
 * Cruises the workspace with the rule set in `.dependency-cruiser.mjs`.
 *
 * @param {string} root the workspace root
 * @returns {Promise<{output: string, exitCode: number}>} the `err` report and its exit code
 */
export async function cruiseWorkspace(root) {
  const {scanRoots} = workspaceLayout(root);
  const options = await extractDepcruiseOptions(join(root, CONFIG_FILE));
  // The resolver reads the tsconfig `paths` aliases from the rule set's options, and the
  // transpiler needs the parsed file; both want an absolute path, so the gate does not depend on
  // the working directory it was started from.
  const tsConfigFile = join(root, options.tsConfig.fileName);
  const result = await cruise(
    scanRoots,
    {
      ...options,
      baseDir: root,
      outputType: 'err',
      tsConfig: {fileName: tsConfigFile},
      ruleSet: {...options.ruleSet, options: {tsConfig: {fileName: tsConfigFile}}},
    },
    undefined,
    {tsConfig: extractTSConfig(tsConfigFile)},
  );
  return {output: result.output, exitCode: result.exitCode};
}

async function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const layout = workspaceLayout(root);
  const errors = declarationErrors(root, layout);
  if (errors.length > 0) {
    console.error('Workspace declarations disagree with the packages on disk:');
    for (const error of errors) console.error(`- ${error}`);
    return 1;
  }
  const {output, exitCode} = await cruiseWorkspace(root);
  process.stdout.write(output);
  return exitCode;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
