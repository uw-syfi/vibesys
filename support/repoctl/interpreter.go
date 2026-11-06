package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"repoctl/execution"
	"repoctl/internal/doctor"
)

// repositoryPythonName is the command the policy's repository scripts name. A check whose command
// is this bare name runs with the repository's interpreter, not with whichever copy PATH holds
// first: a machine's system Python is often older than the repository's code can parse.
const repositoryPythonName = "python3"

// interpreterHost is what finding an interpreter needs from the machine.
type interpreterHost interface {
	Exists(path string) bool
	// Version is what the executable at path reports ("3.12.3"), "" when it cannot be read.
	Version(path string) string
	// PathDirectories are the directories of PATH, in order.
	PathDirectories() []string
}

// repositoryInterpreter chooses the interpreter for repository scripts: the repository's own
// virtual environment when it satisfies, then the first copy on PATH that does. The remedy is
// part of the error, so a run stops at once with what to do instead of failing deep in a script
// with a syntax error.
func repositoryInterpreter(root, minimum, remedy string, host interpreterHost) (string, error) {
	var candidates []string
	candidates = append(candidates, filepath.Join(root, ".venv", "bin", repositoryPythonName))
	for _, dir := range host.PathDirectories() {
		if filepath.IsAbs(dir) {
			candidates = append(candidates, filepath.Join(dir, repositoryPythonName))
		}
	}
	var seen []string
	for _, path := range candidates {
		if !host.Exists(path) {
			continue
		}
		version := host.Version(path)
		if version != "" && doctor.CompareVersions(version, minimum) >= 0 {
			return path, nil
		}
		seen = append(seen, fmt.Sprintf("%s (%s)", path, orUnreadable(version)))
	}
	found := "no python3 was found"
	if len(seen) > 0 {
		found = "found only " + strings.Join(seen, ", ")
	}
	return "", fmt.Errorf("the repository's scripts need Python >= %s, but %s. %s", minimum, found, remedy)
}

func orUnreadable(version string) string {
	if version == "" {
		return "version unreadable"
	}
	return version
}

// pythonRunner runs checks with the repository's interpreter in place of a bare python3.
type pythonRunner struct {
	inner   execution.Runner
	root    string
	minimum string
	remedy  string
	host    interpreterHost
	chosen  string
}

// resolve returns the check with its interpreter chosen. The choice is made once.
func (r *pythonRunner) resolve(check execution.Check) (execution.Check, error) {
	if len(check.Args) == 0 || check.Args[0] != repositoryPythonName {
		return check, nil
	}
	if r.chosen == "" {
		path, err := repositoryInterpreter(r.root, r.minimum, r.remedy, r.host)
		if err != nil {
			return check, fmt.Errorf("%s: %w", check.Label, err)
		}
		r.chosen = path
	}
	check.Args = append([]string{r.chosen}, check.Args[1:]...)
	return check, nil
}

// Preflight fails before anything runs when a check needs an interpreter the machine lacks.
func (r *pythonRunner) Preflight(checks []execution.Check) error {
	for _, check := range checks {
		if _, err := r.resolve(check); err != nil {
			return err
		}
	}
	return nil
}

// Run implements execution.Runner.
func (r *pythonRunner) Run(ctx context.Context, root string, check execution.Check) error {
	resolved, err := r.resolve(check)
	if err != nil {
		return err
	}
	return r.inner.Run(ctx, root, resolved)
}

// preflighter is a Runner that can refuse a whole plan before running any of it.
type preflighter interface {
	Preflight([]execution.Check) error
}

// withRepositoryPython wraps a runner so bare python3 commands use the repository's interpreter.
// The version comes from the doctor policy's python3 entry; a repository without one runs its
// commands unchanged.
func withRepositoryPython(inner execution.Runner, root, configPath string) execution.Runner {
	cfg, err := doctor.ReadConfig(policyDirectory(root, configPath))
	if err != nil {
		return inner
	}
	for _, program := range cfg.Programs {
		if program.Name != repositoryPythonName || program.MinimumFrom == nil {
			continue
		}
		minimum, err := doctor.ReadPin(doctor.OSHost{Root: root}, root, *program.MinimumFrom)
		if err != nil || minimum == "" {
			return inner
		}
		remedy := "Put Python " + minimum + " or newer first on PATH."
		if len(program.Fix) > 0 {
			remedy = "Run ./support/repoctl/repoctl doctor --fix (it runs `" + strings.Join(program.Fix, " ") +
				"` to create .venv), or put Python " + minimum + " or newer first on PATH."
		}
		return &pythonRunner{inner: inner, root: root, minimum: minimum, remedy: remedy, host: osInterpreterHost{}}
	}
	return inner
}

type osInterpreterHost struct{}

func (osInterpreterHost) Exists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func (osInterpreterHost) Version(path string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, path, "--version").CombinedOutput()
	if err != nil {
		return ""
	}
	return doctor.ParseVersion(string(out))
}

func (osInterpreterHost) PathDirectories() []string {
	return filepath.SplitList(os.Getenv("PATH"))
}
