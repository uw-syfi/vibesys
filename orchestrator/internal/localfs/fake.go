package localfs

import (
	"fmt"
	"strings"
)

// Fake is a Detector with a table. A path is on the filesystem of the longest mount that contains
// it, and "/" is local unless the table says otherwise, as on a real host.
type Fake struct {
	// Mounts maps a mount point to its filesystem.
	Mounts map[string]Filesystem
	// Unreadable lists paths whose detection fails.
	Unreadable map[string]bool
}

// Detect implements Detector.
func (f Fake) Detect(path string) (Filesystem, error) {
	if f.Unreadable[path] {
		return Filesystem{}, fmt.Errorf("statfs %s: permission denied", path)
	}
	best, found := "", false
	for mount := range f.Mounts {
		inside := path == mount || strings.HasPrefix(path, strings.TrimSuffix(mount, "/")+"/")
		if inside && (!found || len(mount) > len(best)) {
			best, found = mount, true
		}
	}
	if !found {
		return Filesystem{Type: "ext4"}, nil
	}
	return f.Mounts[best], nil
}
