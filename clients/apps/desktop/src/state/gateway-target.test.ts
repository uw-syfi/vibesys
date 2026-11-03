import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {gatewayTarget} from './gateway-target.js';

describe('the gateway target', () => {
  it('reads the same platform from uname and from Node', () => {
    expect(gatewayTarget('Linux\nx86_64\n')).toBe('linux-amd64');
    expect(gatewayTarget('linux x64')).toBe('linux-amd64');
    expect(gatewayTarget('Linux\naarch64')).toBe('linux-arm64');
    expect(gatewayTarget('Darwin\narm64')).toBe('darwin-arm64');
    expect(gatewayTarget('darwin arm64')).toBe('darwin-arm64');
  });

  it('refuses every other platform, naming it', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('Darwin x86_64', 'Windows_NT x64', 'FreeBSD amd64', 'Linux riscv64'),
        fc.constantFrom('', '\n', '  '),
        (output, padding) => {
          expect(() => gatewayTarget(`${padding}${output}${padding}`)).toThrow(
            `unsupported gateway platform: ${output}`,
          );
        },
      ),
    );
  });
});
