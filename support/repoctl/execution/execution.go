// Package execution defines checks independently of CI policy and language adapters.
// A check runs one argument-vector command from a repository-relative directory.
// Callers select checks; Runner owns the process and reports command failures.
package execution

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
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
	Name            string            `toml:"name"`
	TriggerJob      string            `toml:"trigger_job"`
	IncludeInTest   bool              `toml:"include_in_test"`
	Language        string            `toml:"language"`
	Directory       string            `toml:"directory"`
	AlwaysCommands  [][]string        `toml:"always_commands"`
	Commands        [][]string        `toml:"commands"`
	PackageCommands [][]string        `toml:"package_commands"`
	Collection      string            `toml:"collection"`
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

// Commands builds checks for a group with no collection expansion.
func Commands(suite Suite) ([]Check, error) {
	return checksForCommands(suite, append(append([][]string{}, suite.AlwaysCommands...), suite.Commands...))
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

// OSRunner runs checks with inherited standard output and error streams.
type OSRunner struct{}

func (OSRunner) Run(parent context.Context, root string, check Check) error {
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
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
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
