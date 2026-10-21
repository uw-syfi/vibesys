// Package runindex resolves only the host's published run symlink index.
package runindex

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
)

var runID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// Resolve returns the indexed directory's run.log, including when the file has
// not been created yet. It rejects traversal and entries that are not symlinks.
func Resolve(root, id string) (string, error) {
	if !runID.MatchString(id) {
		return "", fmt.Errorf("invalid run_id %q", id)
	}
	entry := filepath.Join(root, "runs", id)
	info, err := os.Lstat(entry)
	if err != nil {
		return "", err
	}
	if info.Mode()&os.ModeSymlink == 0 {
		return "", fmt.Errorf("run index entry %q is not a symlink", id)
	}
	target, err := filepath.EvalSymlinks(entry)
	if err != nil {
		return "", err
	}
	info, err = os.Stat(target)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", fmt.Errorf("run index entry %q does not target a directory", id)
	}
	return filepath.Join(target, "run.log"), nil
}
