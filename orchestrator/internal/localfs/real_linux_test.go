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

// TestRealDetectorAgreesWithTheMountTable runs the real detector over the directories of this host
// and compares each answer with the mount table. On the development host /home is NFS and
// /mnt/data is local, so both answers are exercised there; on another host the paths simply are what
// they are.
func TestRealDetectorAgreesWithTheMountTable(t *testing.T) {
	cwd, _ := os.Getwd()
	for _, path := range []string{"/", "/home", "/mnt/data", "/tmp", "/var", "/dev/shm", cwd} {
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
		if fs.Network != isNetworkMount(fstype) {
			t.Errorf("%s: mount table says %s, detector says network=%v", path, fstype, fs.Network)
		}
	}
}

func TestRealResolveRefusesNetworkAndAcceptsLocal(t *testing.T) {
	for _, path := range []string{"/home", "/mnt/data", "/dev/shm"} {
		real, err := filepath.EvalSymlinks(path)
		if err != nil {
			continue
		}
		fstype, ok := mountType(t, real)
		if !ok {
			t.Skip("no mount table on this host")
		}
		_, err = localfs.Resolve(filepath.Join(real, "vibesys-state-that-does-not-exist"), localfs.SystemDetector{})
		if isNetworkMount(fstype) != (err != nil) {
			t.Errorf("%s (%s): Resolve error = %v", path, fstype, err)
		}
	}
}
