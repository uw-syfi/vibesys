package doctor

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// FakeHost is an in-memory Host: a file system of paths, a table of mounts, environment and git
// configuration, and programs that print what they are given. It is the Fake the tests and the
// command wiring use in place of OSHost.
type FakeHost struct {
	Env map[string]string
	// Files maps an absolute path to its content; a directory is any parent of a file.
	Files map[string]string
	// Dirs lists directories that exist with no file in them.
	Dirs map[string]bool
	// Mounts maps a mount point to its file system; the longest mount point holding a path wins.
	Mounts map[string]Filesystem
	// Versions maps an executable's absolute path to what `--version` prints for it.
	Versions map[string]string
	Config   map[string]string
	Linked   bool
	// Plan is returned by PlanChanges.
	Plan error
	// RunFails makes Execute of a run action fail with this error.
	RunFails error
	// Executed records every action performed, in order.
	Executed []Action
	// OnExecute, when set, may change the fake after an action (an install creating files).
	OnExecute func(*FakeHost, Action)
}

// NewFakeHost returns an empty fake.
func NewFakeHost() *FakeHost {
	return &FakeHost{
		Env: map[string]string{}, Files: map[string]string{}, Dirs: map[string]bool{},
		Mounts: map[string]Filesystem{}, Versions: map[string]string{}, Config: map[string]string{},
	}
}

// Getenv implements Host.
func (h *FakeHost) Getenv(name string) string { return h.Env[name] }

// Exists implements Host.
func (h *FakeHost) Exists(path string) bool {
	path = filepath.Clean(path)
	if _, ok := h.Files[path]; ok || h.Dirs[path] {
		return true
	}
	for f := range h.Files {
		if strings.HasPrefix(f, path+"/") {
			return true
		}
	}
	for d := range h.Dirs {
		if strings.HasPrefix(d, path+"/") {
			return true
		}
	}
	return false
}

// ReadFile implements Host.
func (h *FakeHost) ReadFile(path string) ([]byte, error) {
	if s, ok := h.Files[filepath.Clean(path)]; ok {
		return []byte(s), nil
	}
	return nil, &os.PathError{Op: "open", Path: path, Err: os.ErrNotExist}
}

// Run implements Host: only `<executable> --version` is known.
func (h *FakeHost) Run(dir string, argv ...string) (string, error) {
	if len(argv) == 2 && argv[1] == "--version" {
		if out, ok := h.Versions[argv[0]]; ok {
			return out, nil
		}
	}
	return "", fmt.Errorf("fake: no program for %v", argv)
}

// Filesystem implements Host.
func (h *FakeHost) Filesystem(path string) (Filesystem, error) {
	path = filepath.Clean(path)
	points := make([]string, 0, len(h.Mounts))
	for p := range h.Mounts {
		points = append(points, p)
	}
	sort.Slice(points, func(i, j int) bool { return len(points[i]) > len(points[j]) })
	for _, p := range points {
		if isUnder(path, p) {
			return h.Mounts[p], nil
		}
	}
	return Filesystem{}, errors.New("fake: no mount holds " + path)
}

// GitConfig implements Host.
func (h *FakeHost) GitConfig(key string) string { return h.Config[key] }

// LinkedWorktree implements Host.
func (h *FakeHost) LinkedWorktree() bool { return h.Linked }

// PlanChanges implements Host.
func (h *FakeHost) PlanChanges() error { return h.Plan }

// Execute implements Host: it records the action and applies its effect on the fake.
func (h *FakeHost) Execute(a Action) error {
	h.Executed = append(h.Executed, a)
	switch a.Kind {
	case ActionRun:
		if h.RunFails != nil {
			return h.RunFails
		}
	case ActionRemoveTree:
		prefix := filepath.Clean(a.Path)
		for f := range h.Files {
			if f == prefix || strings.HasPrefix(f, prefix+"/") {
				delete(h.Files, f)
			}
		}
		for d := range h.Dirs {
			if d == prefix || strings.HasPrefix(d, prefix+"/") {
				delete(h.Dirs, d)
			}
		}
	case ActionMakeDirectory:
		h.Dirs[filepath.Clean(a.Path)] = true
	case ActionSetGitConfig:
		h.Config[a.Key] = a.Value
	}
	if h.OnExecute != nil {
		h.OnExecute(h, a)
	}
	return nil
}
