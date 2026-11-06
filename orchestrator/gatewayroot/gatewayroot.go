// Package gatewayroot says which directory a host serves from, so that the installer, the host and
// its clients, which are separate programs, agree on it without sharing anything else. A root has
// one host: one socket, one lock, one configuration and one state directory.
package gatewayroot

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// EnvHome is the setting the installer and every client read for the root.
const EnvHome = "VIBESYS_HOME"

// Rule is the one statement of how a root is chosen, printed whenever two answers could differ.
const Rule = "the root is --root when given, else " + EnvHome + " when set, else the directory this program is installed in (the one holding releases/), else ~/.vibesys; " + EnvHome + " must name the directory this program is installed in"

// Error is a root that cannot be chosen without guessing. It says what to change.
type Error struct{ Problem string }

func (e *Error) Error() string { return e.Problem + ": " + Rule }

// Installed is the root a program belongs to because of where it is installed, or "" when it is in
// no installed release.
func Installed(program string) string {
	rel, err := release.FinderFor(program)()
	if err != nil {
		return ""
	}
	return rel.InstallHome()
}

// For is the root a program uses, by Rule. installed is what Installed says of the program, and home
// the user's home directory. The result has its symlinks resolved as far as the path exists, so a
// home reached through a link and the same home reached directly are one root.
func For(flag string, getenv func(string) string, home, installed string) (string, error) {
	root, err := place(flag, strings.TrimSpace(getenv(EnvHome)), home, installed)
	if err != nil {
		return "", err
	}
	return resolveExisting(root), nil
}

func place(flag, setting, home, installed string) (string, error) {
	switch {
	case flag != "":
		return filepath.Abs(flag)
	case setting != "":
		if !filepath.IsAbs(setting) {
			return "", &Error{Problem: fmt.Sprintf("%s=%q is not an absolute path", EnvHome, setting)}
		}
		if installed != "" && resolveExisting(setting) != resolveExisting(installed) {
			return "", &Error{Problem: fmt.Sprintf("%s=%s, and this program is installed in %s", EnvHome, setting, installed)}
		}
		return filepath.Clean(setting), nil
	case installed != "":
		return installed, nil
	case home == "":
		return "", &Error{Problem: "there is no home directory for the default root"}
	default:
		return filepath.Join(home, defaultRootName), nil
	}
}

const defaultRootName = ".vibesys"

// StateDirectory is where the host of root keeps the orchestrator's state when its configuration
// does not say. Under the default root it is the XDG place it has always been, so an existing host
// finds its runs; any other root keeps its state inside itself, so that a root an operator chose
// because it is on a local disk has its state there too, and two roots never meet on one state.
func StateDirectory(root, home string, getenv func(string) string) string {
	if home != "" && root == resolveExisting(filepath.Join(home, defaultRootName)) {
		return localfs.DefaultPath(getenv, home)
	}
	return filepath.Join(root, "state")
}

// CheckState says whether a host of root could keep its state where chosen says, or where its root
// says when chosen is empty, as the host itself would find when it started: a local disk, a place a
// directory can be made by this user, and, for a directory the operator chose, a path short enough for the sockets. It changes nothing.
func CheckState(chosen, root, home string, getenv func(string) string) error {
	state, err := localfs.StateDir(chosen, StateDirectory(root, home, getenv), localfs.SystemDetector{})
	if err != nil {
		return err
	}
	if _, err := state.AgentSocket(); err != nil {
		return err
	}
	if err := state.CheckSessionSockets(); err != nil {
		return err
	}
	existing := state.Path()
	for {
		info, err := os.Stat(existing)
		if err == nil {
			if !info.IsDir() {
				return fmt.Errorf("the state directory %s cannot be made: %s is not a directory; %s", state.Path(), existing, localfs.Remedy)
			}
			break
		}
		parent := filepath.Dir(existing)
		if !errors.Is(err, os.ErrNotExist) || parent == existing {
			return fmt.Errorf("the state directory %s: %w; %s", state.Path(), err, localfs.Remedy)
		}
		existing = parent
	}
	if err := unix.Access(existing, unix.W_OK|unix.X_OK); err != nil {
		return fmt.Errorf("the state directory %s cannot be written: %s: %w; %s", state.Path(), existing, err, localfs.Remedy)
	}
	return nil
}

// resolveExisting resolves the symlinks of the longest existing prefix of path and keeps the rest.
func resolveExisting(path string) string {
	if real, err := filepath.EvalSymlinks(path); err == nil {
		return real
	}
	parent := filepath.Dir(path)
	if parent == path {
		return path
	}
	return filepath.Join(resolveExisting(parent), filepath.Base(path))
}
