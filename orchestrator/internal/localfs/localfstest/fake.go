// Package localfstest holds the Fake detector for tests.
//
// A Fake proves any path local, so it can mint a localfs.Dir for a place the real check would
// refuse. Only test files may import this package; archcheck enforces it.
package localfstest

import (
	"fmt"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

// Fake is a localfs.Detector with a table. A path is on the filesystem of the longest mount that contains
// it, and "/" is local unless the table says otherwise, as on a real host.
type Fake struct {
	// Mounts maps a mount point to its filesystem.
	Mounts map[string]localfs.Filesystem
	// Unreadable lists paths whose detection fails.
	Unreadable map[string]bool
}

// Detect implements localfs.Detector.
func (f Fake) Detect(path string) (localfs.Filesystem, error) {
	if f.Unreadable[path] {
		return localfs.Filesystem{}, fmt.Errorf("statfs %s: permission denied", path)
	}
	best, found := "", false
	for mount := range f.Mounts {
		inside := path == mount || strings.HasPrefix(path, strings.TrimSuffix(mount, "/")+"/")
		if inside && (!found || len(mount) > len(best)) {
			best, found = mount, true
		}
	}
	if !found {
		return localfs.Filesystem{Type: "ext4", Class: localfs.Local}, nil
	}
	return f.Mounts[best], nil
}
