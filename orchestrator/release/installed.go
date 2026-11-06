package release

import (
	"os"
	"path/filepath"
)

// FinderFor is the Finder of a program on the real file system. It follows the program's own path
// through symlinks, and reads the release folder afresh on every call, so a release repaired in
// place is seen at once.
func FinderFor(program string) Finder {
	return func() (*Release, error) {
		resolved, err := filepath.EvalSymlinks(program)
		if err != nil {
			resolved = program
		}
		return Find(resolved, os.DirFS("/"))
	}
}

// OpenDir reads the release in dir on the real file system.
func OpenDir(dir string) (*Release, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, err
	}
	return Open(abs, os.DirFS("/"))
}
