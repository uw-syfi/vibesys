/** The platforms a gateway binary is built for, and the names the host and Node call them. */

/** Every platform a host program is built for, in the order a build lists them. */
export const GATEWAY_TARGETS = ['linux-amd64', 'linux-arm64', 'darwin-arm64'] as const;

export type GatewayTarget = (typeof GATEWAY_TARGETS)[number];

/**
 * The target named by `uname -s; uname -m` output (or Node's `platform arch` pair), or an error
 * naming what was reported.
 */
export function gatewayTarget(output: string): GatewayTarget {
  const [os, arch] = output.trim().split(/\s+/);
  const target = `${os?.toLowerCase()}-${arch === 'x86_64' || arch === 'x64' ? 'amd64' : arch === 'aarch64' ? 'arm64' : arch}`;
  const known = GATEWAY_TARGETS.find(candidate => candidate === target);
  if (known !== undefined) return known;
  throw new Error(`unsupported gateway platform: ${output.trim()}`);
}
