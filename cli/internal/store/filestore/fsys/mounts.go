package fsys

import (
	"bytes"
	"fmt"
	"slices"
	"strings"
)

// CheckMount reads a Linux mount table, the content of /proc/self/mountinfo, and fails with
// ErrNoLocks when the mount of the device major:minor is one whose locks stop at this machine.
// That is an NFS mount with local_lock=flock, local_lock=all or nolock: the lock looks whole from
// here, so no test on this machine can find out. A line it cannot read says nothing about any
// device and is skipped.
func CheckMount(table []byte, major, minor uint32) error {
	device := fmt.Sprintf("%d:%d", major, minor)
	for line := range bytes.Lines(table) {
		// The fields, from proc(5): id, parent, major:minor, root, mount point, options, then
		// optional fields up to a lone "-", then the filesystem type, the source, and the options
		// of the filesystem.
		fields := strings.Fields(string(line))
		tail := slices.Index(fields, "-")
		if tail < 6 || len(fields) < tail+4 || fields[2] != device {
			continue
		}
		if kind := fields[tail+1]; kind != "nfs" && kind != "nfs4" {
			continue
		}
		for _, option := range strings.Split(fields[tail+3], ",") {
			switch option {
			case "local_lock=flock", "local_lock=all", "nolock":
				return fmt.Errorf("%w: %s is an NFS mount with %s, whose locks stop at this machine", ErrNoLocks, fields[4], option)
			}
		}
	}
	return nil
}
