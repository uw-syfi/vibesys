#!/usr/bin/env node
// Run `buf breaking` against the proto/ tree of a base git ref.
//
// The base ref defaults to origin/main and can be set with PROTO_BASE_REF. When
// the base ref has no buf.yaml (the change that introduces it), there
// is nothing to compare against and the check passes with a notice.
//
// A deliberate incompatible change is a new package version (proto/.../v3),
// not an edit that trips this check.
import {spawnSync} from 'node:child_process';

const baseRef = process.env.PROTO_BASE_REF ?? 'origin/main';

const listing = spawnSync('git', ['ls-tree', '--name-only', baseRef, 'buf.yaml'], {
  encoding: 'utf8',
});
if (listing.status !== 0) {
  console.error(`check_proto_breaking: cannot resolve ${baseRef}: ${listing.stderr.trim()}`);
  process.exit(2);
}
if (!listing.stdout.trim()) {
  console.log(`check_proto_breaking: ${baseRef} has no buf.yaml; nothing to compare.`);
  process.exit(0);
}

const result = spawnSync('buf', ['breaking', '--against', `.git#ref=${baseRef}`], {
  stdio: 'inherit',
});
process.exit(result.status ?? 1);
