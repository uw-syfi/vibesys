package task

import (
	"path"
	"slices"
	"sort"
	"strings"
)

// generatedDirs are directory names whose contents a tool writes, never an author.
var generatedDirs = []string{"__pycache__", ".pytest_cache"}

// generatedSuffixes are file name endings of files a tool writes, never an author.
var generatedSuffixes = []string{".pyc", ".pyo"}

// Generated says whether a path from the task's directory is a file a tool generates, such as
// Python's compiled modules, and so is not part of the task. A commit that happens to hold one
// (a checker was run in the checkout and the bytecode was committed with it) must not make the
// task another version: the version stands for what the task does, and these files change when
// the interpreter runs. It reports on the path alone and matches any directory of it, so a
// directory that is Generated hides everything under it.
func Generated(rel string) bool {
	dirs, name := path.Split(rel)
	for _, d := range strings.Split(strings.Trim(dirs, "/"), "/") {
		if slices.Contains(generatedDirs, d) {
			return true
		}
	}
	if slices.Contains(generatedDirs, name) {
		return true
	}
	for _, suffix := range generatedSuffixes {
		if strings.HasSuffix(name, suffix) {
			return true
		}
	}
	return false
}

// Change is one file the versions of two tasks disagree on.
type Change struct {
	// Path is the file's path from the repository root.
	Path string
	// Kind is "added" when the task has a file its base lacks, "removed" when the base has one the
	// task lacks, and "changed" when both have it with different content or executable bit.
	Kind string
}

func (c Change) String() string { return c.Path + " (" + c.Kind + ")" }

// ChangesFrom lists the files of t that differ from base's, by path, for two tasks of one name:
// empty exactly when they have one Version. A file t has and base lacks is "added", one base has
// and t lacks is "removed".
func (t Task) ChangesFrom(base Task) []Change {
	dir := path.Join(TasksDir, t.Name) + "/"
	var changes []Change
	for p, f := range t.Files {
		switch b, ok := base.Files[p]; {
		case !ok:
			changes = append(changes, Change{dir + p, "added"})
		case b != f:
			changes = append(changes, Change{dir + p, "changed"})
		}
	}
	for p := range base.Files {
		if _, ok := t.Files[p]; !ok {
			changes = append(changes, Change{dir + p, "removed"})
		}
	}
	sort.Slice(changes, func(i, j int) bool { return changes[i].Path < changes[j].Path })
	return changes
}
