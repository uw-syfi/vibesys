import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {DesktopMachineSetRepositoriesSchema, validatedFromBinary} from '@vibesys/proto';
import fc from 'fast-check';
import {checkRepositoriesFolder, REPOSITORIES_FIELD} from './repositories-folder.js';

/** The contract's own verdict on a folder, through its message and protovalidate. */
const contractAccepts = (directory: string): boolean => {
  try {
    const bytes = toBinary(
      DesktopMachineSetRepositoriesSchema,
      create(DesktopMachineSetRepositoriesSchema, {
        host: 'local',
        repositoriesDirectory: directory,
      }),
    );
    validatedFromBinary(DesktopMachineSetRepositoriesSchema, bytes);
    return true;
  } catch {
    return false;
  }
};

const text = fc.oneof(
  fc.string({maxLength: 30}),
  fc
    .tuple(fc.constantFrom('/', '~', '~/', '~x', ' /', ' ~/ ', ''), fc.string({maxLength: 30}))
    .map(([head, tail]) => head + tail),
  fc.string({minLength: 1020, maxLength: 1030}).map(rest => `/${rest}`),
);

describe('the Repositories folder', () => {
  it('is accepted exactly when the contract accepts what it keeps', () => {
    fc.assert(
      fc.property(text, input => {
        const check = checkRepositoriesFolder(input);
        if (check.ok) expect(contractAccepts(check.directory)).toBe(true);
        else expect(contractAccepts(input.trim())).toBe(false);
      }),
      {numRuns: 500},
    );
  });

  it('names the field in every refusal', () => {
    fc.assert(
      fc.property(text, input => {
        const check = checkRepositoriesFolder(input);
        if (!check.ok) expect(check.message.startsWith(REPOSITORIES_FIELD)).toBe(true);
      }),
    );
  });

  it('keeps a trimmed folder and treats blank as none', () => {
    expect(checkRepositoriesFolder('  ~/repos ')).toEqual({ok: true, directory: '~/repos'});
    expect(checkRepositoriesFolder('   ')).toEqual({ok: true, directory: ''});
    expect(checkRepositoriesFolder('repos').ok).toBe(false);
    expect(checkRepositoriesFolder('~user/x').ok).toBe(false);
  });
});
