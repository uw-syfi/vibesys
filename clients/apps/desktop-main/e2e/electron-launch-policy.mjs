const ALLOW_UNSANDBOXED_CHROMIUM_ENV =
  'VIBESYS_ELECTRON_E2E_DISABLE_CHROMIUM_SANDBOX';

/**
 * Return process arguments required to launch the downloaded Electron test runtime.
 *
 * Root cannot use Chromium's SUID helper. GitHub's hosted runner also installs that helper
 * without the root ownership and mode Chromium requires, so CI opts out explicitly. This policy
 * is confined to the E2E executable and never changes a production BrowserWindow configuration.
 */
export function electronRuntimeArgs({
  platform = process.platform,
  isRoot = process.getuid?.() === 0,
  allowUnsandboxedChromium =
    process.env[ALLOW_UNSANDBOXED_CHROMIUM_ENV] === '1',
} = {}) {
  return platform === 'linux' && (isRoot || allowUnsandboxedChromium)
    ? ['--no-sandbox']
    : [];
}
