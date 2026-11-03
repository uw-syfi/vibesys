package production

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// EnvCLI names the setting that chooses the vibesys-cli every agent of this daemon runs: the
// absolute path of an executable file.
const EnvCLI = "VIBESYS_ORCHESTRATOR_CLI"

// cliName is the program the skills run.
const cliName = "vibesys-cli"

// Files answers what ResolveCLI asks of the file system.
type Files interface {
	// Executable says path is a regular file the user may run.
	Executable(path string) bool
}

// OSFiles is the real file system.
type OSFiles struct{}

// Executable implements Files.
func (OSFiles) Executable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0
}

// ResolveCLI picks the vibesys-cli the daemon gives its agents, once, as an absolute path, so that
// what an agent runs does not depend on the PATH its shell ends up with (a login shell re-reads the
// user's profile and may put another build first). In order:
//
//  1. The setting EnvCLI. It is an operator's explicit choice: a relative path, or one that is not
//     an executable file, is an error naming the setting, and never replaced by another build.
//  2. A vibesys-cli next to the daemon's own executable, the build that was installed with it.
//  3. The first vibesys-cli on the daemon's own PATH, which is the build its operator's shell runs.
//
// With none of them it is an error naming the setting: the skills have no other way to find the CLI,
// so a daemon with nothing to give would start runs that cannot work.
func ResolveCLI(getenv func(string) string, executable string, files Files) (string, error) {
	if setting := strings.TrimSpace(getenv(EnvCLI)); setting != "" {
		switch {
		case !filepath.IsAbs(setting):
			return "", fmt.Errorf("%s=%q must be an absolute path", EnvCLI, setting)
		case !files.Executable(setting):
			return "", fmt.Errorf("%s=%s is not an executable file", EnvCLI, setting)
		}
		return filepath.Clean(setting), nil
	}
	candidates := []string{}
	if executable != "" {
		candidates = append(candidates, filepath.Join(filepath.Dir(executable), cliName))
	}
	for _, dir := range filepath.SplitList(getenv("PATH")) {
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
	return "", fmt.Errorf("no vibesys-cli found beside the daemon or on its PATH; set %s to the absolute path of the one to use", EnvCLI)
}

// sessionEnv is the environment of a sidecar process: the daemon's own, with the CLI the daemon
// chose under EnvCLI and nothing else of the launch environment. A value inherited under that name
// is dropped first, so an agent never runs a build the daemon did not choose; when the daemon chose
// none, the name is unset.
func sessionEnv(base []string, launch map[string]string, name string) []string {
	out := make([]string, 0, len(base)+1)
	for _, kv := range base {
		if !strings.HasPrefix(kv, name+"=") {
			out = append(out, kv)
		}
	}
	if v := launch[name]; v != "" {
		out = append(out, name+"="+v)
	}
	return out
}
