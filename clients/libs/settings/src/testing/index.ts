/**
 * Test support for settings consumers: the Node filesystem with injectable platform durability,
 * and an in-memory filesystem with matching faults and crash-recovery boundaries.
 */

export {
  type DirectorySyncCapability,
  NodeSettingsFilesystem,
  type NodeSettingsFilesystemOptions,
} from '../settings-store.js';
export {FakeSettingsFilesystem, fileError} from './fake-settings-filesystem.js';
