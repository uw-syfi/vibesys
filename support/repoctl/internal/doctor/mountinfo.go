package doctor

import (
	"strconv"
	"strings"
)

// networkFilesystems are file system types whose locking, rename and socket semantics differ
// from a local disk's enough to break Go tests, unix sockets and local clones.
var networkFilesystems = map[string]bool{
	"nfs": true, "nfs3": true, "nfs4": true, "cifs": true, "smb3": true, "smbfs": true,
	"afs": true, "9p": true, "lustre": true, "gpfs": true, "beegfs": true, "ceph": true,
	"glusterfs": true, "fuse.sshfs": true, "fuse.nfs": true, "fuse.glusterfs": true,
	"fuse.gcsfuse": true, "fuse.s3fs": true,
}

// IsNetworkFilesystem reports whether the file system type is a network one.
func IsNetworkFilesystem(fsType string) bool { return networkFilesystems[fsType] }

// FilesystemFromMountInfo returns the file system type of the mount that holds path, read from
// the text of /proc/self/mountinfo: the type of the mount with the longest mount point that is
// path or a parent of it. path must be absolute and clean. It returns "" when no mount matches.
func FilesystemFromMountInfo(mountinfo, path string) string {
	best, bestType := -1, ""
	for _, line := range strings.Split(mountinfo, "\n") {
		before, after, ok := strings.Cut(line, " - ")
		if !ok {
			continue
		}
		fields := strings.Fields(before)
		rest := strings.Fields(after)
		if len(fields) < 5 || len(rest) < 1 {
			continue
		}
		point := unescapeMountField(fields[4])
		if !isUnder(path, point) || len(point) <= best {
			continue
		}
		best, bestType = len(point), rest[0]
	}
	return bestType
}

func isUnder(path, dir string) bool {
	if dir == "/" {
		return strings.HasPrefix(path, "/")
	}
	return path == dir || strings.HasPrefix(path, dir+"/")
}

// unescapeMountField decodes the octal escapes (\040 for a space) mountinfo writes.
func unescapeMountField(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+3 < len(s) {
			if n, err := strconv.ParseUint(s[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(n))
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}
