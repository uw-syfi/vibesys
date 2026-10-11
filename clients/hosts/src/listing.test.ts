import {describe, expect, test} from 'bun:test';
import {instancePhaseText} from './listing.js';

describe('instancePhaseText', () => {
  test('a starting run reads as its stage and progress, so a long image build is visible', () => {
    expect(
      instancePhaseText({
        status: 'starting',
        stage: 'building_image',
        progress: {done: 12, total: 30, label: 'RUN pip install'},
      }),
    ).toBe('Building image 12/30: RUN pip install');
    expect(
      instancePhaseText({
        status: 'starting',
        stage: 'building_image',
        progress: {done: 3, total: null},
      }),
    ).toBe('Building image 3/?');
    expect(instancePhaseText({status: 'starting', stage: 'resolving'})).toBe('Resolving');
  });

  test('any other phase reads as its status', () => {
    for (const status of ['running', 'stopping', 'stopped', 'failed', 'completed'] as const) {
      expect(instancePhaseText({status})).toBe(status);
    }
  });
});
