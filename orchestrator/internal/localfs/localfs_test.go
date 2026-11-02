package localfs_test

import (
	"errors"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

var (
	local = localfs.Filesystem{Type: "ext4"}
	nfs   = localfs.Filesystem{Type: "nfs4", Network: true}
)

func noEnv(string) string { return "" }

func TestEveryNetworkTypeIsRefusedAndNamed(t *testing.T) {
	linux := map[string]int64{
		"nfs": 0x6969, "smb": 0x517b, "smb2": 0xfe534d42, "cifs": 0xff534d42, "ceph": 0xc36400, "afs": 0x5346414f,
		"fuse": 0x65735546, "9p": 0x1021997, "lustre": 0x0bd00bd0,
	}
	darwin := []string{"nfs", "smbfs", "afpfs", "webdav"}
	var cases []localfs.Filesystem
	for name, magic := range linux {
		fs := localfs.ClassifyLinux(magic)
		if !fs.Network || fs.Type != name {
			t.Errorf("linux magic 0x%x: got %+v, want a network %s", magic, fs, name)
		}
		cases = append(cases, fs)
	}
	for _, name := range darwin {
		fs := localfs.ClassifyDarwin(name)
		if !fs.Network {
			t.Errorf("darwin %s: want network, got %+v", name, fs)
		}
		cases = append(cases, fs)
	}
	for _, fs := range cases {
		dir := t.TempDir()
		_, err := localfs.Resolve(filepath.Join(dir, "state"), localfs.Fake{Mounts: map[string]localfs.Filesystem{dir: fs}})
		var refusal *localfs.NetworkError
		if !errors.As(err, &refusal) {
			t.Fatalf("%s: want a NetworkError, got %v", fs.Type, err)
		}
		for _, want := range []string{filepath.Join(dir, "state"), fs.Type, localfs.EnvStateDir, "local"} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("%s: error %q does not say %q", fs.Type, err, want)
			}
		}
	}
}

func TestLocalFilesystemsPassWhatEverTheirType(t *testing.T) {
	for _, name := range []string{"apfs", "hfs", "ufs"} {
		if localfs.ClassifyDarwin(name).Network {
			t.Errorf("darwin %s is local", name)
		}
	}
	for _, magic := range []int64{0xef53, 0x58465342, 0x9123683e, 0x01021994, 0x794c7630, 0x2fc12fc1} { // ext4 xfs btrfs tmpfs overlayfs zfs
		if localfs.ClassifyLinux(magic).Network {
			t.Errorf("linux magic 0x%x is local", magic)
		}
	}
	// Property: a type is a network type exactly when the table names it, so no unlisted number is
	// ever refused and no listed one passes.
	r := rand.New(rand.NewPCG(1, 2))
	listed := map[int64]bool{}
	for _, m := range []int64{0x6969, 0x517b, 0xfe534d42, 0xff534d42, 0xc36400, 0x5346414f, 0x73757245, 0x564c,
		0x1021997, 0x65735546, 0x0bd00bd0, 0x1161970, 0x7461636f, 0xaad7aaea, 0x19830326, 0x786f4256} {
		listed[m] = true
		if !localfs.ClassifyLinux(m).Network {
			t.Errorf("0x%x should be a network type", m)
		}
	}
	for range 2000 {
		m := r.Int64N(1 << 33)
		if got := localfs.ClassifyLinux(m).Network; got != listed[m] {
			t.Fatalf("0x%x: network=%v, listed=%v", m, got, listed[m])
		}
	}
}

func TestADirectoryThatDoesNotExistYetIsJudgedByItsNearestAncestor(t *testing.T) {
	dir := t.TempDir()
	detector := localfs.Fake{Mounts: map[string]localfs.Filesystem{dir: local}}
	got, err := localfs.Resolve(filepath.Join(dir, "a", "b", "state"), detector)
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(dir, "a", "b", "state"); !strings.HasSuffix(got.Path(), filepath.Join("a", "b", "state")) || got.Path() == "" {
		t.Errorf("Path() = %q, want it to end like %q", got.Path(), want)
	}
	if _, err := os.Stat(filepath.Join(dir, "a")); !os.IsNotExist(err) {
		t.Errorf("Resolve created a directory: %v", err)
	}
}

func TestASymlinkIsFollowedBeforeTheCheck(t *testing.T) {
	root := t.TempDir()
	remote := filepath.Join(root, "remote")
	if err := os.Mkdir(remote, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link")
	if err := os.Symlink(remote, link); err != nil {
		t.Fatal(err)
	}
	real, err := filepath.EvalSymlinks(remote)
	if err != nil {
		t.Fatal(err)
	}
	detector := localfs.Fake{Mounts: map[string]localfs.Filesystem{real: nfs}}
	_, err = localfs.Resolve(filepath.Join(link, "state"), detector)
	var refusal *localfs.NetworkError
	if !errors.As(err, &refusal) {
		t.Fatalf("a link into NFS passed: %v", err)
	}
	if refusal.Resolved != filepath.Join(real, "state") {
		t.Errorf("Resolved = %q, want the path past the link %q", refusal.Resolved, filepath.Join(real, "state"))
	}
	if !strings.Contains(err.Error(), "resolved to") {
		t.Errorf("the error does not show where the link leads: %v", err)
	}
	// And a link out of NFS into a local disk passes, and the proof holds the real path.
	local, err := localfs.Resolve(filepath.Join(link, "state"), localfs.Fake{Mounts: map[string]localfs.Filesystem{root: nfs, real: {Type: "ext4"}}})
	if err != nil {
		t.Fatal(err)
	}
	if local.Path() != filepath.Join(real, "state") {
		t.Errorf("Path() = %q, want %q", local.Path(), filepath.Join(real, "state"))
	}
}

func TestADetectorThatFailsRefusesRatherThanGuesses(t *testing.T) {
	dir := t.TempDir()
	real, _ := filepath.EvalSymlinks(dir)
	_, err := localfs.Resolve(dir, localfs.Fake{Unreadable: map[string]bool{real: true}})
	if err == nil || !strings.Contains(err.Error(), localfs.EnvStateDir) {
		t.Fatalf("want a refusal naming %s, got %v", localfs.EnvStateDir, err)
	}
}

func TestTheDefaultDirectoryIsRefusedNotRelocated(t *testing.T) {
	home := t.TempDir()
	realHome, _ := filepath.EvalSymlinks(home)
	detector := localfs.Fake{Mounts: map[string]localfs.Filesystem{realHome: nfs}}
	_, err := localfs.StateDir(noEnv, home, detector)
	var refusal *localfs.NetworkError
	if !errors.As(err, &refusal) || !refusal.Default {
		t.Fatalf("want the default refused as a default, got %v", err)
	}
	if !strings.Contains(err.Error(), "default state directory") || !strings.Contains(err.Error(), ".local/state/vibesys/orchestrator") {
		t.Errorf("the error does not name the default: %v", err)
	}
	// The variable moves it, to a local disk, and nothing else does.
	elsewhere := t.TempDir()
	got, err := localfs.StateDir(func(k string) string {
		if k == localfs.EnvStateDir {
			return filepath.Join(elsewhere, "state")
		}
		return ""
	}, home, localfs.Fake{Mounts: map[string]localfs.Filesystem{realHome: nfs}})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(got.Path(), filepath.Join("state")) {
		t.Errorf("Path() = %q", got.Path())
	}
}

func TestTheDefaultPathFollowsXDGWhenItIsAbsolute(t *testing.T) {
	get := func(k string) string {
		if k == "XDG_STATE_HOME" {
			return "/var/lib/me"
		}
		return ""
	}
	if got, want := localfs.DefaultPath(get, "/home/x"), "/var/lib/me/vibesys/orchestrator"; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
	rel := func(k string) string {
		if k == "XDG_STATE_HOME" {
			return "relative"
		}
		return ""
	}
	if got, want := localfs.DefaultPath(rel, "/home/x"), "/home/x/.local/state/vibesys/orchestrator"; got != want {
		t.Errorf("a relative XDG_STATE_HOME must be ignored: got %q, want %q", got, want)
	}
}

func TestARelativeStateDirectoryVariableIsRejected(t *testing.T) {
	_, err := localfs.StateDir(func(k string) string {
		if k == localfs.EnvStateDir {
			return "state"
		}
		return ""
	}, "/home/x", localfs.Fake{})
	if err == nil || !strings.Contains(err.Error(), localfs.EnvStateDir) {
		t.Fatalf("want a rejection naming the variable, got %v", err)
	}
}
