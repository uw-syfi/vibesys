/** The registry record a fake detached server publishes, in the server's own JSON shape. */
export function fakeRecord(id: string, socketPath: string, protocolVersion = 1): object {
  return {
    version: 1,
    id,
    status: 'serving',
    socket_path: socketPath,
    project_root: '/home/user/project',
    run_id: null,
    pid: 4242,
    started_at: 1_700_000_000,
    hostname: 'node-1',
    protocol_version: protocolVersion,
    vibesys_version: '0.0.0+fake',
  };
}
