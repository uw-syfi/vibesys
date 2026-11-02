// Package execution defines checks independently of CI policy and language adapters.
// A check runs one argument-vector command from a repository-relative directory.
// Callers select checks; Runner owns the process and reports command failures.
package execution

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Check is one configured command. Args are passed directly to the executable,
// without shell expansion. Directory is relative to the repository root.
type Check struct {
	Label     string
	Directory string
	Args      []string
	Env       map[string]string
	Timeout   time.Duration
}

// Suite is a policy-owned named group of checks. Collection names an impact-plan
// collection; PackageCommands run once for each value. Commands run when the
// collection is empty, or when no collection is set.
type Suite struct {
	Name          string `toml:"name"`
	TriggerJob    string `toml:"trigger_job"`
	IncludeInTest bool   `toml:"include_in_test"`
	// IncludeInChecks selects the group in `repoctl test` when TriggerJob is
	// selected, and in `repoctl test --checks-only`. An empty TriggerJob
	// selects it on every change. Mutually exclusive with IncludeInTest.
	IncludeInChecks bool `toml:"include_in_checks"`
	// LocalSkip keeps the group out of `repoctl test` and is the reason
	// `--dry-run` prints. CI still runs the group. Requires TriggerJob.
	LocalSkip       string     `toml:"local_skip"`
	Language        string     `toml:"language"`
	Directory       string     `toml:"directory"`
	AlwaysCommands  [][]string `toml:"always_commands"`
	Commands        [][]string `toml:"commands"`
	PackageCommands [][]string `toml:"package_commands"`
	// GateCommands are the fast subset of the group that `repoctl gate` (the pre-push
	// hook) runs once, in place of its other commands. A group with none is not part of
	// the gate. CI never reads them: its jobs run the full commands.
	GateCommands [][]string `toml:"gate_commands"`
	Collection   string     `toml:"collection"`
	// LocalPaths narrows `repoctl test` only (never `run-checks`, which CI
	// uses): when set, the group runs locally only if a changed path equals or
	// is under one of these repository-relative paths.
	LocalPaths []string `toml:"local_paths"`
	// LocalComponents extends LocalPaths with the roots and files of the named
	// components, so a path list that CI also selects on is declared once, in
	// components.toml, and cannot drift from this group's local selection.
	LocalComponents []string          `toml:"local_components"`
	Env             map[string]string `toml:"env"`
	TimeoutSeconds  int               `toml:"timeout_seconds"`
}

// Planner validates and translates a configured suite and selected collection
// values to executable checks. It must preserve configured command order.
type Planner interface {
	Validate(Suite, map[string]bool) error
	Plan(Suite, []string) ([]Check, error)
}

// ValidateCommands rejects collection settings for languages that run only
// the configured commands.
func ValidateCommands(suite Suite) error {
	if suite.Collection != "" || len(suite.PackageCommands) != 0 {
		return fmt.Errorf("check_groups.%s: collection commands are unsupported for language %q", suite.Name, suite.Language)
	}
	return nil
}

// ValidatePackageCommands checks a suite for a language that expands a collection:
// the collection must exist, package_commands and the collection come together,
// and every package command names {package}.
func ValidatePackageCommands(suite Suite, collections map[string]bool) error {
	if suite.Collection != "" && (!collections[suite.Collection] || len(suite.PackageCommands) == 0) {
		return fmt.Errorf("check_groups.%s: unknown collection or missing package_commands", suite.Name)
	}
	if suite.Collection == "" && len(suite.PackageCommands) != 0 {
		return fmt.Errorf("check_groups.%s: package_commands require a collection", suite.Name)
	}
	for _, args := range suite.PackageCommands {
		found := false
		for _, arg := range args {
			found = found || strings.Contains(arg, "{package}")
		}
		if !found {
			return fmt.Errorf("check_groups.%s: package command requires {package}", suite.Name)
		}
	}
	return nil
}

// PackageChecks plans a collection-expanding suite. With no selected values it
// runs the suite's Commands; otherwise it runs AlwaysCommands, then every
// package command once per value, in sorted value order, with {package} replaced.
func PackageChecks(suite Suite, packages []string) ([]Check, error) {
	if len(packages) == 0 {
		return Commands(suite)
	}
	checks, err := Always(suite)
	if err != nil {
		return nil, err
	}
	packages = append([]string(nil), packages...)
	sort.Strings(packages)
	for _, name := range packages {
		for _, configured := range suite.PackageCommands {
			if len(configured) == 0 || configured[0] == "" {
				return nil, fmt.Errorf("check group %q has an empty package command", suite.Name)
			}
			args := make([]string, len(configured))
			for i, arg := range configured {
				args[i] = strings.ReplaceAll(arg, "{package}", name)
			}
			checks = append(checks, Check{
				Label: suite.Name + ":" + name, Directory: suite.Directory,
				Args: args, Env: suite.Env,
				Timeout: time.Duration(suite.TimeoutSeconds) * time.Second,
			})
		}
	}
	return checks, nil
}

// Commands builds checks for a group with no collection expansion.
func Commands(suite Suite) ([]Check, error) {
	return checksForCommands(suite, append(append([][]string{}, suite.AlwaysCommands...), suite.Commands...))
}

// ForGate returns the suite with only its gate commands, run once and never per package.
func (s Suite) ForGate() Suite {
	s.AlwaysCommands = nil
	s.Commands = s.GateCommands
	s.PackageCommands = nil
	s.Collection = ""
	return s
}

// Always builds checks which run before any selected collection values.
func Always(suite Suite) ([]Check, error) {
	return checksForCommands(suite, suite.AlwaysCommands)
}

func checksForCommands(suite Suite, commands [][]string) ([]Check, error) {
	checks := make([]Check, 0, len(commands))
	for _, args := range commands {
		if len(args) == 0 || args[0] == "" {
			return nil, fmt.Errorf("check group %q has an empty command", suite.Name)
		}
		checks = append(checks, Check{
			Label: suite.Name, Directory: suite.Directory,
			Args: append([]string(nil), args...), Env: suite.Env,
			Timeout: time.Duration(suite.TimeoutSeconds) * time.Second,
		})
	}
	return checks, nil
}

// Runner executes checks. Implementations should honor cancellation and return
// errors for missing executables and nonzero exits.
type Runner interface {
	Run(context.Context, string, Check) error
}

// OSRunner runs checks, writing to Stdout and Stderr, which default to the process's own.
type OSRunner struct {
	Stdout, Stderr io.Writer
	// OwnProcessGroup runs each check in a process group of its own that cancellation
	// kills as a whole. Set it when the output is captured: a descendant that outlives
	// its parent otherwise keeps the capture open and Run waits for it.
	OwnProcessGroup bool
}

func (r OSRunner) Run(parent context.Context, root string, check Check) error {
	if len(check.Args) == 0 || check.Args[0] == "" {
		return fmt.Errorf("%s: empty command", check.Label)
	}
	ctx := parent
	cancel := func() {}
	if check.Timeout > 0 {
		ctx, cancel = context.WithTimeout(parent, check.Timeout)
	}
	defer cancel()
	cmd := exec.CommandContext(ctx, check.Args[0], check.Args[1:]...)
	cmd.Dir = filepath.Join(root, filepath.FromSlash(check.Directory))
	cmd.Env = mergedEnvironment(os.Environ(), check.Env)
	if r.OwnProcessGroup {
		ownProcessGroup(cmd)
		cmd.WaitDelay = 5 * time.Second
	}
	cmd.Stdout, cmd.Stderr = r.Stdout, r.Stderr
	if cmd.Stdout == nil {
		cmd.Stdout = os.Stdout
	}
	if cmd.Stderr == nil {
		cmd.Stderr = os.Stderr
	}
	err := cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return fmt.Errorf("%s: %s: timed out after %s", check.Label, strings.Join(check.Args, " "), check.Timeout)
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if err != nil {
		return fmt.Errorf("%s: %s: %w", check.Label, strings.Join(check.Args, " "), err)
	}
	return nil
}

func mergedEnvironment(base []string, overrides map[string]string) []string {
	if len(overrides) == 0 {
		return base
	}
	env := make([]string, 0, len(base)+len(overrides))
	for _, entry := range base {
		key, _, _ := strings.Cut(entry, "=")
		if _, replaced := overrides[key]; !replaced {
			env = append(env, entry)
		}
	}
	for key, value := range overrides {
		env = append(env, key+"="+value)
	}
	return env
}
