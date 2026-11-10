package production

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// SettingJobTool is the field of the host's configuration that chooses the vibesys-cli every agent of
// this daemon runs, in place of the release's.
const SettingJobTool = "job_tool"

// cliName is the program the skills run.
const cliName = "vibesys-cli"

// Files answers what the daemon's path resolution asks of the file system.
type Files interface {
	// Executable says path is a regular file the user may run.
	Executable(path string) bool
}

// OSFiles is the real file system.
type OSFiles struct{}

// Executable implements Files.
func (f OSFiles) Executable(path string) bool {
	mode, ok := f.mode(path)
	return ok && mode.IsRegular() && mode.Perm()&0o111 != 0
}

func (OSFiles) mode(path string) (os.FileMode, bool) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, false
	}
	return info.Mode(), true
}

// ResolveCLI picks the vibesys-cli the daemon gives its agents, once, as an absolute path, so that
// what an agent runs does not depend on the PATH its shell ends up with (a login shell re-reads the
// user's profile and may put another build first). In order:
//
//  1. The setting SettingJobTool. It is an operator's explicit choice: a relative path, or one that is not
//     an executable file, is an error naming the setting, and never replaced by another build.
//  2. The job tool of the release the daemon is installed in. A release whose tool is missing is an
//     error naming the file, never answered with another build.
//  3. The first vibesys-cli in the host's tool path. Only a daemon that is in no release (a
//     developer's build) gets here.
//
// With none of them it is an error naming the setting: the skills have no other way to find the CLI,
// so a daemon with nothing to give would start runs that cannot work.
func ResolveCLI(setting string, toolPath []string, find release.Finder, files Files) (string, error) {
	if setting = strings.TrimSpace(setting); setting != "" {
		switch {
		case !filepath.IsAbs(setting):
			return "", fmt.Errorf("%s=%q must be an absolute path", SettingJobTool, setting)
		case !files.Executable(setting):
			return "", fmt.Errorf("%s=%s is not an executable file", SettingJobTool, setting)
		}
		return filepath.Clean(setting), nil
	}
	rel, err := find()
	var notInstalled *release.NotInstalledError
	switch {
	case err == nil:
		return rel.JobTool()
	case !errors.As(err, &notInstalled):
		return "", err
	}
	var candidates []string
	for _, dir := range toolPath {
		// A relative entry means the working directory of whoever looks, which is not a build anyone
		// chose.
		if filepath.IsAbs(dir) {
			candidates = append(candidates, filepath.Join(dir, cliName))
		}
	}
	for _, path := range candidates {
		if files.Executable(path) {
			return path, nil
		}
	}
	return "", fmt.Errorf("no vibesys-cli: %w, and none in the host's tool path; choose the one to use (%s) with `vibesys-client host configure --job-tool PATH`, then `vibesys-client host restart`", err, SettingJobTool)
}
