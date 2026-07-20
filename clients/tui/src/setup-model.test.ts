import {describe, expect, it} from 'vitest';
import {
  applySetupSelection,
  parseSetupDefaults,
  shouldOfferInteractiveSetup,
  validateSetupSelection,
} from './setup-model.js';

describe('interactive setup model', () => {
  const selection = {
    inputPath: '/repo/examples/queue-spsc',
    experimentName: 'queue-spsc-20260720-120000',
    repositoryOwner: 'vibesys-playground',
    repositoryName: 'queue-spsc-20260720-120000',
    visibility: 'private',
  };

  it('parses backend defaults', () => {
    expect(
      parseSetupDefaults(
        JSON.stringify({
          input_path: selection.inputPath,
          experiment_name: selection.experimentName,
          repository_owner: selection.repositoryOwner,
          repository_name: selection.repositoryName,
          visibility: selection.visibility,
        }),
      ),
    ).toMatchObject({repository_owner: 'vibesys-playground', visibility: 'private'});
  });

  it('replaces launch values with the confirmed form values', () => {
    expect(
      applySetupSelection(['--input=old', '--exp-name', 'old-name', '--backend', 'cpu'], selection),
    ).toEqual([
      '--backend',
      'cpu',
      '--input',
      selection.inputPath,
      '--exp-name',
      selection.experimentName,
      '--repo',
      `${selection.repositoryOwner}/${selection.repositoryName}`,
      '--repo-visibility',
      'private',
    ]);
  });

  it('allows clearing the owner for a local-only experiment', () => {
    const local = {...selection, repositoryOwner: ''};

    expect(validateSetupSelection(local)).toBeUndefined();
    expect(applySetupSelection([], local)).not.toContain('--repo');
  });

  it('validates repository and required fields', () => {
    expect(validateSetupSelection({...selection, inputPath: ''})).toContain('Input bundle');
    expect(validateSetupSelection({...selection, repositoryOwner: 'bad/owner'})).toContain('owner');
    expect(validateSetupSelection({...selection, visibility: 'secret'})).toContain('Visibility');
  });

  it('skips setup for resume, explicit repositories, and smoke runs', () => {
    expect(shouldOfferInteractiveSetup(['--input', 'example'])).toBe(true);
    expect(shouldOfferInteractiveSetup(['--resume'])).toBe(false);
    expect(shouldOfferInteractiveSetup(['--repo=owner/name'])).toBe(false);
    expect(shouldOfferInteractiveSetup(['--stub-agent'])).toBe(false);
  });
});
