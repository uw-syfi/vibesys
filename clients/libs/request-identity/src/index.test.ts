import {expect, test} from 'bun:test';
import {newRequestId} from '@vibesys/request-identity';

for (const count of [1, 2, 7, 100]) {
  test(`fresh request identities use UUIDs and do not collide across ${count} calls`, () => {
    const ids = Array.from({length: count}, () => newRequestId());
    expect(new Set(ids).size).toBe(count);
    for (const id of ids) {
      expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    }
  });
}
