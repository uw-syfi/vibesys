export interface ElectronRuntimeEnvironment {
  readonly platform?: NodeJS.Platform;
  readonly isRoot?: boolean;
  readonly allowUnsandboxedChromium?: boolean;
}

export declare function electronRuntimeArgs(
  environment?: ElectronRuntimeEnvironment,
): readonly string[];
