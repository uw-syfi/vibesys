/** The platforms a gateway binary is built for, and the names the host and Node call them. */

export type GatewayTarget = 'linux-amd64' | 'linux-arm64' | 'darwin-arm64';

/**
 * The target named by `uname -s; uname -m` output (or Node's `platform arch` pair), or an error
 * naming what was reported.
 */
export function gatewayTarget(output: string): GatewayTarget {
  const [os, arch] = output.trim().split(/\s+/);
  const target = `${os?.toLowerCase()}-${arch === 'x86_64' || arch === 'x64' ? 'amd64' : arch === 'aarch64' ? 'arm64' : arch}`;
  if (target === 'linux-amd64' || target === 'linux-arm64' || target === 'darwin-arm64')
    return target;
  throw new Error(`unsupported gateway platform: ${output.trim()}`);
}
