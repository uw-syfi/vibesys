package assemble

import (
	"context"
	"fmt"
	"io/fs"
	"path"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// Sink is where the release is laid out. Names are relative to the release folder and use "/".
type Sink interface {
	// MkdirAll makes the folder and its parents.
	MkdirAll(dir string) error
	// WriteFile makes or replaces a file.
	WriteFile(name string, data []byte, perm fs.FileMode) error
}

// Pin is an external repository at one revision.
type Pin struct {
	URL      string
	Revision string
}

// Externals gives the files of an example that lives in another repository. The repository pins the
// other repository to a revision; the files are those of that revision.
type Externals interface {
	// Fetch returns the example's own files at the pinned revision, rooted at the example's folder
	// (so they hold ".vibesys/tasks/..."), and the pin. Only those files are carried; the rest of
	// the repository is cloned when an example is provisioned.
	Fetch(ctx context.Context, e Entry) (own fs.FS, pin Pin, err error)
}

// Staged is an example laid into the release.
type Staged struct {
	Entry
	// Tasks are the task names the example holds and the registry lists, in order.
	Tasks []string
	// Pin is where the rest of an external example is cloned from; nil for one that is all here.
	Pin *Pin
}

// tasksFolder is where an example's tasks are, in its folder.
const tasksFolder = ".vibesys/tasks"

// skipped names are never carried: editors' and interpreters' leftovers.
var skipped = []string{".DS_Store", "__pycache__"}

// Stage lays each example of the registry into sink under examples/ID and returns what it laid
// down, in the registry's order. repo is the repository's checkout, rooted at its top.
//
// What it carries is what a person who copied the folder would want and nothing private:
//   - no .git, and no folder that holds one: a nested repository (a submodule's checkout, a vendored
//     clone) is the other repository's, not the example's;
//   - no link, device or socket: a link's target could be anywhere on the build machine;
//   - for an external example, only its own files, as Externals gives them.
//
// It fails naming the example and the file, and writes nothing for an example it fails on after the
// first file; a caller discards the stage on any error.
func Stage(ctx context.Context, repo fs.FS, entries []Entry, ext Externals, sink Sink) ([]Staged, error) {
	var staged []Staged
	for _, e := range entries {
		var (
			source fs.FS
			pin    *Pin
			err    error
		)
		if e.External {
			own, p, ferr := ext.Fetch(ctx, e)
			if ferr != nil {
				return nil, fmt.Errorf("example %s: %w", e.ID(), ferr)
			}
			if err := checkPin(p); err != nil {
				return nil, fmt.Errorf("example %s: %w", e.ID(), err)
			}
			if source, err = fs.Sub(own, ".vibesys"); err != nil {
				return nil, fmt.Errorf("example %s: %w", e.ID(), err)
			}
			pin = &p
		} else if source, err = fs.Sub(repo, e.Path); err != nil {
			return nil, fmt.Errorf("example %s: %w", e.ID(), err)
		}
		dest := path.Join(release.ExamplesRoot, e.ID())
		if e.External {
			dest = path.Join(dest, ".vibesys")
		}
		if err := copyTree(source, dest, sink); err != nil {
			return nil, fmt.Errorf("example %s: %w", e.ID(), err)
		}
		tasks, err := tasksOf(source, e)
		if err != nil {
			return nil, fmt.Errorf("example %s: %w", e.ID(), err)
		}
		staged = append(staged, Staged{Entry: e, Tasks: tasks, Pin: pin})
	}
	return staged, nil
}

func checkPin(p Pin) error {
	if p.URL == "" || strings.HasPrefix(p.URL, "-") || len(p.Revision) != 40 || strings.Trim(p.Revision, "0123456789abcdef") != "" {
		return fmt.Errorf("the pin %+v is not a URL and a full commit id", p)
	}
	return nil
}

// tasksOf is the example's task names: the registry's, each checked to be there, or every folder
// under .vibesys/tasks. source is rooted at the example's folder, or at its .vibesys for an
// external example.
func tasksOf(source fs.FS, e Entry) ([]string, error) {
	root := tasksFolder
	if e.External {
		root = "tasks"
	}
	dirs, err := fs.ReadDir(source, root)
	if err != nil {
		return nil, fmt.Errorf("it has no %s: %w", tasksFolder, err)
	}
	var on []string
	for _, d := range dirs {
		if d.IsDir() {
			on = append(on, d.Name())
		}
	}
	if e.Tasks == nil {
		if len(on) == 0 {
			return nil, fmt.Errorf("it has no task under %s", tasksFolder)
		}
		return on, nil // ReadDir sorts by name.
	}
	for _, t := range e.Tasks {
		if !slices.Contains(on, t) {
			return nil, fmt.Errorf("the registry lists task %q and %s has none", t, tasksFolder)
		}
	}
	return slices.Clone(e.Tasks), nil
}

func copyTree(src fs.FS, dest string, sink Sink) error {
	return fs.WalkDir(src, ".", func(name string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		base := path.Base(name)
		if slices.Contains(skipped, base) {
			if d.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if base == ".git" {
			return fmt.Errorf("%s: a nested repository is not carried", name)
		}
		target := path.Join(dest, name)
		switch {
		case d.IsDir():
			if name != "." {
				if _, err := fs.Stat(src, path.Join(name, ".git")); err == nil {
					return fs.SkipDir // The other repository's, whose checkout this happens to hold.
				}
			}
			return sink.MkdirAll(target)
		case d.Type().IsRegular():
			info, err := d.Info()
			if err != nil {
				return err
			}
			data, err := fs.ReadFile(src, name)
			if err != nil {
				return err
			}
			perm := fs.FileMode(0o644)
			if info.Mode().Perm()&0o111 != 0 {
				perm = 0o755
			}
			return sink.WriteFile(target, data, perm)
		}
		return fmt.Errorf("%s: only files and folders are carried, not %s", name, d.Type())
	})
}
