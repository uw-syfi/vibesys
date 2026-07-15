package main

import (
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
)

var (
	runnerOnce sync.Once
	runnerPath string
	runnerErr  error
)

func nativeRunnerPath() (string, error) {
	runnerOnce.Do(func() {
		if configured := os.Getenv("VIBESYS_QUEUE_NATIVE_RUNNER"); configured != "" {
			runnerPath, runnerErr = filepath.Abs(configured)
			if runnerErr == nil {
				runnerErr = validateRunnerExecutable(runnerPath)
			}
			return
		}

		cwd, err := os.Getwd()
		if err != nil {
			runnerErr = fmt.Errorf("resolve native runner source: %w", err)
			return
		}
		source := filepath.Join(cwd, "native_runner")
		manifest := filepath.Join(source, "Cargo.toml")
		if _, err := os.Stat(manifest); err != nil {
			runnerErr = fmt.Errorf("native runner manifest %q: %w", manifest, err)
			return
		}

		digest := sha256.Sum256([]byte(source))
		target := filepath.Join(
			os.TempDir(),
			fmt.Sprintf("vibesys-queue-native-%x", digest[:8]),
		)
		command := exec.Command(
			"cargo",
			"build",
			"--quiet",
			"--release",
			"--locked",
			"--manifest-path",
			manifest,
			"--target-dir",
			target,
		)
		command.Dir = source
		log := newBoundedLog(64 * 1024)
		command.Stdout = log
		command.Stderr = log
		if err := command.Run(); err != nil {
			runnerErr = fmt.Errorf(
				"build trusted native runner: %w\ncargo output:\n%s",
				err,
				log.String(),
			)
			return
		}
		runnerPath = filepath.Join(target, "release", "vibesys-queue-native-runner")
		runnerErr = validateRunnerExecutable(runnerPath)
	})
	return runnerPath, runnerErr
}

func validateRunnerExecutable(path string) error {
	stat, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("trusted native runner %q: %w", path, err)
	}
	if stat.IsDir() || stat.Mode()&0o111 == 0 {
		return fmt.Errorf("trusted native runner %q is not executable", path)
	}
	return nil
}
