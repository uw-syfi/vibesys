package production

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// EnvCLI names the setting that chooses the vibesys-cli every agent of this daemon runs: the
// absolute path of an executable file.
const EnvCLI = "VIBESYS_ORCHESTRATOR_CLI"

// cliName is the program the skills run.
const cliName = "vibesys-cli"

// Files answers what the daemon's path resolution asks of the file system.
type Files interface {
	// Executable says path is a regular file the user may run.
	Executable(path string) bool
	// Regular says path is a regular file.
	Regular(path string) bool
}

// OSFiles is the real file system.
type OSFiles struct{}

// Executable implements Files.
func (f OSFiles) Executable(path string) bool {
	mode, ok := f.mode(path)
	return ok && mode.IsRegular() && mode.Perm()&0o111 != 0
}

// Regular implements Files.
func (f OSFiles) Regular(path string) bool {
	mode, ok := f.mode(path)
	return ok && mode.IsRegular()
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

// LocationFor is the CliLocation the daemon records on each run it starts: the vibesys-cli it
// chose, and the places that build reads its daemon and store from, taken from the daemon's own
// environment. The vibesys-cli resolves its own places from the same three names and treats one
// that is not an absolute path as unset, so such a value is recorded as unset too: an agent then
// resolves the place as the daemon would have.
func LocationFor(getenv func(string) string, home, cli string) core.CliLocation {
	absolute := func(path string) string {
		if filepath.IsAbs(path) {
			return filepath.Clean(path)
		}
		return ""
	}
	return core.CliLocation{
		CLI: absolute(cli), RuntimeDir: absolute(getenv("XDG_RUNTIME_DIR")),
		StateHome: absolute(getenv("XDG_STATE_HOME")), Home: absolute(home),
	}
}
