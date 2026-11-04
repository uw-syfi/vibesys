/** The hosts a window was opened to read: one `host` parameter per host key, set by the main process. */
import {type HostKey, hostFromKey, hostKey, hostLabel} from '../state/index.js';

export interface WindowHost {
  readonly key: HostKey;
  readonly label: string;
}

/** The distinct hosts the address names, in order. A key that names no host is an error. */
export function hostsOfAddress(search: string): WindowHost[] {
  const seen = new Set<HostKey>();
  const hosts: WindowHost[] = [];
  for (const raw of new URLSearchParams(search).getAll('host')) {
    const id = hostFromKey(raw);
    const key = hostKey(id);
    if (seen.has(key)) continue;
    seen.add(key);
    hosts.push({key, label: hostLabel(id)});
  }
  return hosts;
}
