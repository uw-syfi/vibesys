//go:build linux

package localfs_test

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

// mountType finds the filesystem type the kernel's mount table gives a path, by the longest mount
// point that contains it. It is a second opinion that shares nothing with the detector's statfs
// call, so the two agree only when both are right.
func mountType(t *testing.T, path string) (string, bool) {
	t.Helper()
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return "", false
	}
	defer f.Close()
	best, bestType := "", ""
	scan := bufio.NewScanner(f)
	for scan.Scan() {
		left, right, ok := strings.Cut(scan.Text(), " - ")
		if !ok {
			continue
		}
		fields := strings.Fields(left)
		after := strings.Fields(right)
		if len(fields) < 5 || len(after) < 1 {
			continue
		}
		mount := fields[4]
		inside := path == mount || strings.HasPrefix(path, strings.TrimSuffix(mount, "/")+"/")
		if inside && len(mount) >= len(best) {
			best, bestType = mount, after[0]
		}
	}
	return bestType, best != ""
}

func isNetworkMount(fstype string) bool {
	for _, p := range []string{"nfs", "cifs", "smb", "ceph", "afs", "9p", "fuse", "lustre", "gfs2", "ocfs2"} {
		if strings.HasPrefix(fstype, p) {
			return true
		}
	}
	return false
}

// isLocalMount lists the mount types of this host that the detector must take as local. A host type
// in neither list is not compared: the detector is allowed to refuse what it does not know.
func isLocalMount(fstype string) bool {
	switch fstype {
	case "ext4", "ext3", "ext2", "xfs", "btrfs", "tmpfs", "overlay", "zfs", "f2fs", "ramfs":
		return true
	}
	return false
}

// TestRealDetectorAgreesWithTheMountTable runs the real detector over the directories of this host
// and compares each answer with the mount table. A host that mounts one of these paths over a
// network exercises the refusal; on another host every path is local, and the paths simply are what
// they are.
func TestRealDetectorAgreesWithTheMountTable(t *testing.T) {
	cwd, _ := os.Getwd()
	for _, path := range []string{"/", "/home", "/tmp", "/var", "/dev/shm", cwd} {
		real, err := filepath.EvalSymlinks(path)
		if err != nil {
			continue
		}
		fstype, ok := mountType(t, real)
		if !ok {
			t.Skip("no mount table on this host")
		}
		fs, err := localfs.SystemDetector{}.Detect(real)
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		t.Logf("%-10s mount type %-10s detected %+v", path, fstype, fs)
		switch {
		case isNetworkMount(fstype) && fs.Class != localfs.Network:
			t.Errorf("%s: mount table says %s, detector says %+v, want a network type", path, fstype, fs)
		case isLocalMount(fstype) && fs.Class != localfs.Local:
			t.Errorf("%s: mount table says %s, detector says %+v, want a local type", path, fstype, fs)
		}
	}
}

func TestRealResolveRefusesNetworkAndAcceptsLocal(t *testing.T) {
	for _, path := range []string{"/home", "/tmp", "/dev/shm"} {
		real, err := filepath.EvalSymlinks(path)
		if err != nil {
			continue
		}
		fstype, ok := mountType(t, real)
		if !ok {
			t.Skip("no mount table on this host")
		}
		_, err = localfs.Resolve(filepath.Join(real, "vibesys-state-that-does-not-exist"), localfs.SystemDetector{})
		switch {
		case isNetworkMount(fstype) && err == nil:
			t.Errorf("%s (%s): a network mount passed", path, fstype)
		case isLocalMount(fstype) && err != nil:
			t.Errorf("%s (%s): a local mount was refused: %v", path, fstype, err)
		}
	}
}
