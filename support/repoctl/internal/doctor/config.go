// Package doctor checks that a machine can run this repository's gate and tests, and repairs what
// it safely can: `repoctl doctor`.
//
// The package has a pure core and a thin shell. Gather reads the machine through the Host
// interface into plain Facts, Evaluate turns Facts and the policy into Findings with no I/O, and
// Apply performs a Finding's Actions through the Host. OSHost is the one implementation that
// touches the machine; FakeHost is the in-memory one the tests drive. Every repository-specific
// program, path and pin comes from the policy file (Config); machine-specific values (a better
// TMPDIR, a pinned tool found elsewhere on PATH) are derived from the environment at run time and
// never written down.
package doctor

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/BurntSushi/toml"
)

// ConfigFile is the policy file's name, beside components.toml and checks.toml.
const ConfigFile = "doctor.toml"

// Config is the repository's doctor policy.
type Config struct {
	Hooks        HooksConfig        `toml:"hooks"`
	Temp         TempConfig         `toml:"temp"`
	Programs     []ProgramConfig    `toml:"programs"`
	Installs     []InstallConfig    `toml:"installs"`
	EnvFlags     []EnvFlagConfig    `toml:"env_flags"`
	Instructions InstructionsConfig `toml:"instructions"`
}

// HooksConfig names the tracked hooks directory the pre-push gate lives in.
type HooksConfig struct {
	Path string `toml:"path"`
}

// TempConfig describes the temporary-directory check.
type TempConfig struct {
	// MaxPathLength is the longest TMPDIR that leaves room for a unix socket path (sun_path is
	// 104 or 108 bytes) under it.
	MaxPathLength int `toml:"max_path_length"`
	// Candidates are directories offered as a better TMPDIR, tried in order. `$NAME` is the
	// environment variable NAME; a candidate whose variable is unset is skipped, so a value
	// that is specific to one machine comes from that machine's environment. A candidate must be
	// on a local file system, and one on the repository's file system is preferred.
	Candidates []string `toml:"candidates"`
}

// candidateVariables returns the environment variables the candidates read.
func (t TempConfig) candidateVariables() []string {
	var names []string
	for _, c := range t.Candidates {
		for _, m := range variableReference.FindAllStringSubmatch(c, -1) {
			names = append(names, m[1])
		}
	}
	return names
}

// ProgramConfig is a program the gate or tests run, optionally at a pinned version.
//
// With neither MinimumFrom nor ExactFrom the program only has to be on PATH. Otherwise the first
// copy on PATH must satisfy the version, because that is the copy a script resolves.
type ProgramConfig struct {
	Name string `toml:"name"`
	// MinimumFrom reads the lowest acceptable version from a repository file.
	MinimumFrom *PinConfig `toml:"minimum_from"`
	// ExactFrom reads the one acceptable version from a repository file.
	ExactFrom *PinConfig `toml:"exact_from"`
	// RepoDirectories are repository-relative directories searched for a copy before PATH
	// is, and put first on PATH when the copy there is the one that satisfies.
	RepoDirectories []string `toml:"repo_directories"`
	// Fix is a repository-relative command that installs the program (`--fix` runs it).
	Fix []string `toml:"fix"`
	// Hint tells the reader how to install the program when there is no Fix.
	Hint string `toml:"hint"`
}

// PinConfig names a version recorded in a repository file: Pattern's first capture group.
type PinConfig struct {
	File    string `toml:"file"`
	Pattern string `toml:"pattern"`
}

// InstallConfig is a dependency tree an install command creates. A directory with no Marker was
// installed halfway: a failed or interrupted install leaves the directory without the marker the
// installer writes last.
type InstallConfig struct {
	Name      string   `toml:"name"`
	Directory string   `toml:"directory"`
	Marker    string   `toml:"marker"`
	Command   []string `toml:"command"`
	// CommandDirectory is where Command runs.
	CommandDirectory string `toml:"command_directory"`
}

// EnvFlagConfig requires a flag in an environment variable's space-separated value.
type EnvFlagConfig struct {
	Name string `toml:"name"`
	Flag string `toml:"flag"`
	// LinkedWorktreeOnly limits the requirement to linked git worktrees.
	LinkedWorktreeOnly bool   `toml:"linked_worktree_only"`
	Reason             string `toml:"reason"`
}

// InstructionsConfig lists the agent-facing instruction files whose script references must
// resolve.
type InstructionsConfig struct {
	Files []string `toml:"files"`
	// Roots are repository-relative prefixes; a path under one that the file names must exist.
	Roots []string `toml:"roots"`
}

// ReadConfig reads and validates the policy in policyDir, naming the offending key or path.
func ReadConfig(policyDir string) (Config, error) {
	path := filepath.Join(policyDir, ConfigFile)
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("doctor policy: %w", err)
	}
	return ParseConfig(path, string(raw))
}

// ParseConfig parses policy text; name is used in errors. Unknown keys are rejected.
func ParseConfig(name, text string) (Config, error) {
	var c Config
	md, err := toml.Decode(text, &c)
	if err != nil {
		return Config{}, fmt.Errorf("%s: %w", name, err)
	}
	if un := md.Undecoded(); len(un) > 0 {
		return Config{}, fmt.Errorf("%s: unknown keys %v", name, un)
	}
	if err := c.validate(); err != nil {
		return Config{}, fmt.Errorf("%s: %w", name, err)
	}
	return c, nil
}

func (c Config) validate() error {
	if err := relativePath("hooks.path", c.Hooks.Path); err != nil {
		return err
	}
	if c.Temp.MaxPathLength <= 0 {
		return fmt.Errorf("temp.max_path_length must be positive")
	}
	for _, cand := range c.Temp.Candidates {
		if !strings.HasPrefix(cand, "/") && !strings.HasPrefix(cand, "$") {
			return fmt.Errorf("temp.candidates entry %q must be an absolute path or start with a $VARIABLE", cand)
		}
	}
	seen := map[string]bool{}
	for i, p := range c.Programs {
		key := fmt.Sprintf("programs[%d]", i)
		if p.Name == "" || strings.ContainsAny(p.Name, "/\\ ") {
			return fmt.Errorf("%s.name must be a bare program name, got %q", key, p.Name)
		}
		if seen[p.Name] {
			return fmt.Errorf("%s.name %q is listed twice", key, p.Name)
		}
		seen[p.Name] = true
		if p.MinimumFrom != nil && p.ExactFrom != nil {
			return fmt.Errorf("%s: minimum_from and exact_from are exclusive", key)
		}
		for _, pin := range []struct {
			key string
			pin *PinConfig
		}{{"minimum_from", p.MinimumFrom}, {"exact_from", p.ExactFrom}} {
			if pin.pin == nil {
				continue
			}
			if err := pin.pin.validate(key + "." + pin.key); err != nil {
				return err
			}
		}
		for _, d := range p.RepoDirectories {
			if err := relativePath(key+".repo_directories", d); err != nil {
				return err
			}
		}
	}
	for i, in := range c.Installs {
		key := fmt.Sprintf("installs[%d]", i)
		if in.Name == "" || len(in.Command) == 0 {
			return fmt.Errorf("%s needs a name and a command", key)
		}
		for k, v := range map[string]string{"directory": in.Directory, "marker": in.Marker, "command_directory": in.CommandDirectory} {
			if err := relativePath(key+"."+k, v); err != nil {
				return err
			}
		}
		if !strings.HasPrefix(in.Marker, strings.TrimSuffix(in.Directory, "/")+"/") {
			return fmt.Errorf("%s.marker %q must be inside %q", key, in.Marker, in.Directory)
		}
	}
	for i, f := range c.EnvFlags {
		if f.Name == "" || f.Flag == "" || strings.ContainsAny(f.Flag, " \t") {
			return fmt.Errorf("env_flags[%d] needs a name and a single flag without spaces", i)
		}
	}
	for _, file := range c.Instructions.Files {
		if err := relativePath("instructions.files", file); err != nil {
			return err
		}
	}
	for _, root := range c.Instructions.Roots {
		if err := relativePath("instructions.roots", root); err != nil {
			return err
		}
	}
	return nil
}

func (p PinConfig) validate(key string) error {
	if err := relativePath(key+".file", p.File); err != nil {
		return err
	}
	re, err := regexp.Compile(p.Pattern)
	if err != nil {
		return fmt.Errorf("%s.pattern: %w", key, err)
	}
	if re.NumSubexp() < 1 {
		return fmt.Errorf("%s.pattern must have a capture group around the version", key)
	}
	return nil
}

func relativePath(key, path string) error {
	if path == "" || filepath.IsAbs(path) || path == ".." || strings.HasPrefix(path, "../") {
		return fmt.Errorf("%s must be a repository-relative path, got %q", key, path)
	}
	return nil
}
