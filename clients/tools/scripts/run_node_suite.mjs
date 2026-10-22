#!/usr/bin/env node

/** Run JavaScript test files with the shared Node hang guard and inherited diagnostics. */
import {spawn} from 'node:child_process';
import {NODE_TEST_ARGUMENTS} from './test_runner_config.mjs';

const files = process.argv.slice(2);
if (files.length === 0 || files.some(file => file.startsWith('-'))) {
  console.error('run_node_suite.mjs takes test files only; runner options are shared');
  process.exitCode = 1;
} else {
  const child = spawn(process.execPath, [...NODE_TEST_ARGUMENTS, ...files], {stdio: 'inherit'});
  child.on('error', error => {
    console.error(error.message);
    process.exitCode = 1;
  });
  child.on('close', (status, signal) => {
    if (signal !== null) console.error(`node --test was killed by ${signal}`);
    process.exitCode = signal === null ? (status ?? 1) : 1;
  });
}
