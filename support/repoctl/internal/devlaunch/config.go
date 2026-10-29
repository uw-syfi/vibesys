// Package devlaunch plans and runs `repoctl dev`: build this checkout's desktop app, optionally
// ship the committed HEAD to a machine over ssh, and open the app on a profile of its own.
//
// The package has a pure core and a thin shell. plan.go, fingerprint.go and probe.go decide what to
// do from typed facts and parse what the machine said; run.go gathers the facts and performs the
// steps through the Runner, Clock and Files interfaces, so tests drive the whole flow on Fakes.
// osenv.go holds the real implementations, the only code here that starts a process or reads the
// clock. Every repository-specific command and path comes from the policy file (Config).
package devlaunch

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
)

// ConfigFile is the policy file's name, beside components.toml and checks.toml.
const ConfigFile = "dev.toml"

// Config is the repository's dev launcher policy.
type Config struct {
	Local   LocalConfig   `toml:"local"`
	Machine MachineConfig `toml:"machine"`
}

// LocalConfig says how to install, build and open the app. Directories and paths are relative to
// the repository root; `{profile}` in AppCommand is replaced by the absolute profile directory.
type LocalConfig struct {
	ClientDirectory string   `toml:"client_directory"`
	InstallCommand  []string `toml:"install_command"`
	InstallInputs   []string `toml:"install_inputs"`
	InstallStamp    string   `toml:"install_stamp"`
	// PythonSyncCommand runs in the repository root: the app's This Mac helper starts without syncing.
	PythonSyncCommand []string `toml:"python_sync_command"`
	BuildCommand      []string `toml:"build_command"`
	AppDirectory      string   `toml:"app_directory"`
	AppCommand        []string `toml:"app_command"`
	// AppEnv is added to the app's environment. `{profile}` is the absolute profile directory.
	AppEnv           map[string]string `toml:"app_env"`
	ProfileDirectory string            `toml:"profile_directory"`
}

// MachineConfig says how to ship a commit to a machine and how to start its helper.
type MachineConfig struct {
	// AppHostsFiles are the desktop app's saved hosts files by platform (`~` is the home
	// directory); the first that exists says where each machine's checkout is.
	AppHostsFiles    []string `toml:"app_hosts_files"`
	ShipRef          string   `toml:"ship_ref"`
	SyncCommand      []string `toml:"sync_command"`
	HelperCommand    []string `toml:"helper_command"`
	PathDirectories  []string `toml:"path_directories"`
	HelloSeconds     int      `toml:"hello_seconds"`
	HelloPollSeconds int      `toml:"hello_poll_seconds"`
}

// ReadConfig reads and validates the policy in policyDir, naming the offending key or path.
func ReadConfig(policyDir string) (Config, error) {
	path := filepath.Join(policyDir, ConfigFile)
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("dev policy: %w", err)
	}
	return ParseConfig(path, string(raw))
}

// ParseConfig parses policy text; name is used in errors.
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
	for key, path := range map[string]string{
		"local.client_directory":  c.Local.ClientDirectory,
		"local.app_directory":     c.Local.AppDirectory,
		"local.install_stamp":     c.Local.InstallStamp,
		"local.profile_directory": c.Local.ProfileDirectory,
	} {
		if err := relativePath(key, path); err != nil {
			return err
		}
	}
	for key, argv := range map[string][]string{
		"local.install_command":     c.Local.InstallCommand,
		"local.build_command":       c.Local.BuildCommand,
		"local.python_sync_command": c.Local.PythonSyncCommand,
		"local.app_command":         c.Local.AppCommand,
		"machine.sync_command":      c.Machine.SyncCommand,
		"machine.helper_command":    c.Machine.HelperCommand,
	} {
		if len(argv) == 0 || argv[0] == "" {
			return fmt.Errorf("%s must name a program", key)
		}
	}
	if len(c.Local.InstallInputs) == 0 {
		return fmt.Errorf("local.install_inputs must list at least one path or glob")
	}
	for _, input := range c.Local.InstallInputs {
		if err := relativePath("local.install_inputs", input); err != nil {
			return err
		}
	}
	if !strings.HasPrefix(c.Machine.ShipRef, "refs/") {
		return fmt.Errorf("machine.ship_ref must start with refs/, got %q", c.Machine.ShipRef)
	}
	if c.Machine.HelloSeconds <= 0 || c.Machine.HelloPollSeconds <= 0 {
		return fmt.Errorf("machine.hello_seconds and machine.hello_poll_seconds must be positive")
	}
	if len(c.Machine.AppHostsFiles) == 0 {
		return fmt.Errorf("machine.app_hosts_files must list at least one path")
	}
	return nil
}

func relativePath(key, path string) error {
	if path == "" || filepath.IsAbs(path) || path == ".." || strings.HasPrefix(path, "../") {
		return fmt.Errorf("%s must be a repository-relative path, got %q", key, path)
	}
	return nil
}
