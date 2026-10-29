import {expect, test} from 'bun:test';
import {gatewayTarget} from '../index.js';

test('gateway targets normalize local Node and remote uname platform names', () => {
  for (const os of ['Linux', 'linux']) {
    for (const arch of ['x64', 'x86_64', 'amd64', 'arm64', 'aarch64']) {
      for (const whitespace of [' ', '\n', '\t']) {
        expect(gatewayTarget(`  ${os}${whitespace}${arch}\n`)).toBe(
          arch === 'arm64' || arch === 'aarch64' ? 'linux-arm64' : 'linux-amd64',
        );
      }
    }
  }
  for (const os of ['Darwin', 'darwin']) {
    expect(gatewayTarget(`${os} arm64`)).toBe('darwin-arm64');
  }
});

test('unsupported gateway platforms fail with the reported platform', () => {
  for (const output of ['Linux riscv64', 'Darwin x64', 'win32 x64', '', 'Linux']) {
    expect(() => gatewayTarget(output)).toThrow(`unsupported gateway platform: ${output.trim()}`);
  }
});
