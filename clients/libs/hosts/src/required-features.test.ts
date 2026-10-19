import {describe, expect, it} from 'bun:test';
import type {HelperFeature} from '@vibesys/contracts/host-helper';
import {missingRequiredFeatures, REQUIRED_HELPER_FEATURES} from './required-features.js';

describe('missingRequiredFeatures', () => {
  it('names nothing while the features are not known', () => {
    expect(missingRequiredFeatures(null)).toEqual([]);
  });

  it('names nothing for a helper with every required feature, whatever else it offers', () => {
    const offered: HelperFeature[] = [...REQUIRED_HELPER_FEATURES, 'logs'];
    expect(missingRequiredFeatures(offered)).toEqual([]);
  });

  it('names exactly the required features a helper lacks, for any subset', () => {
    const count = REQUIRED_HELPER_FEATURES.length;
    for (let mask = 0; mask < 2 ** count; mask += 1) {
      const offered = REQUIRED_HELPER_FEATURES.filter((_, index) => (mask & (1 << index)) !== 0);
      const lacking = REQUIRED_HELPER_FEATURES.filter(feature => !offered.includes(feature));
      expect(missingRequiredFeatures(offered)).toEqual(lacking);
    }
  });

  it('requires the run trace, which a helper that predates it does not offer', () => {
    const before = REQUIRED_HELPER_FEATURES.filter(feature => feature !== 'run_trace');
    expect(missingRequiredFeatures(before)).toEqual(['run_trace']);
  });
});
