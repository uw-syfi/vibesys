import {describe, expect, test} from 'bun:test';
import type {SkippedRecent} from '@vibesys/runs';
import {NOTICE_REASONS, recentNotice} from './recent-notice.js';
import {SIDEBAR_EMPTY, sidebarStep} from './sidebar.js';

const skip = (index: number): SkippedRecent => ({
  index,
  reason: `recent.json: runs[${index}].task must be a task name`,
});

describe('the notice for saved runs that could not be read', () => {
  test('is absent when every saved run was read', () => {
    expect(recentNotice([])).toBeNull();
  });

  test('counts every skipped run and names the key of the first few, for any number', () => {
    for (let count = 1; count <= 12; count += 1) {
      const skipped = Array.from({length: count}, (_, index) => skip(index * 3));

      const notice = recentNotice(skipped);

      expect(notice?.title).toBe(`${count} saved ${count === 1 ? 'run' : 'runs'} skipped`);
      for (const [index, entry] of skipped.entries()) {
        expect(notice?.body.includes(entry.reason)).toBe(index < NOTICE_REASONS);
      }
      expect(notice?.body.includes('more')).toBe(count > NOTICE_REASONS);
      if (count > NOTICE_REASONS) {
        expect(notice?.body).toContain(`+${count - NOTICE_REASONS} more`);
      }
    }
  });

  test('the sidebar keeps the notice from the overview until a list the store wrote replaces it', () => {
    const skippedRecent = [skip(16)];
    const heard = sidebarStep(SIDEBAR_EMPTY, {
      type: 'overview',
      listings: [],
      recent: [],
      skippedRecent,
    }).state;
    expect(heard.skippedRecent).toEqual(skippedRecent);

    expect(sidebarStep(heard, {type: 'recent', recent: []}).state.skippedRecent).toEqual([]);
  });
});
