package localfs_test

import (
	"errors"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs/localfstest"
)

var (
	local = localfs.Filesystem{Type: "ext4", Class: localfs.Local}
	nfs   = localfs.Filesystem{Type: "nfs4", Class: localfs.Network}
)

func noEnv(string) string { return "" }

func TestEveryNetworkTypeIsRefusedAndNamed(t *testing.T) {
	linux := map[string]uint32{
		"nfs": 0x6969, "smb": 0x517b, "smb2": 0xfe534d42, "cifs": 0xff534d42, "ceph": 0xc36400, "afs": 0x5346414f,
		"fuse": 0x65735546, "9p": 0x1021997, "lustre": 0x0bd00bd0,
	}
	darwin := []string{"nfs", "smbfs", "afpfs", "webdav"}
	var cases []localfs.Filesystem
	for name, magic := range linux {
		fs := localfs.ClassifyLinux(magic)
		if fs.Class != localfs.Network || fs.Type != name {
			t.Errorf("linux magic 0x%x: got %+v, want a network %s", magic, fs, name)
		}
		cases = append(cases, fs)
	}
	for _, name := range darwin {
		fs := localfs.ClassifyDarwin(name)
		if fs.Class != localfs.Network {
			t.Errorf("darwin %s: want network, got %+v", name, fs)
		}
		cases = append(cases, fs)
	}
	for _, fs := range cases {
		requireRefused(t, fs, "network filesystem")
	}
}

// requireRefused resolves a directory on a filesystem and wants the refusal to name the path, the
// filesystem type, the variable and the fix.
func requireRefused(t *testing.T, fs localfs.Filesystem, kind string) {
	t.Helper()
	dir := t.TempDir()
	_, err := localfs.Resolve(filepath.Join(dir, "state"), localfstest.Fake{Mounts: map[string]localfs.Filesystem{dir: fs}})
	var refusal *localfs.RefusedError
	if !errors.As(err, &refusal) {
		t.Fatalf("%s: want a RefusedError, got %v", fs.Type, err)
	}
	for _, want := range []string{filepath.Join(dir, "state"), fs.Type, localfs.EnvStateDir, "local", kind} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("%s: error %q does not say %q", fs.Type, err, want)
		}
	}
}

func TestEveryKnownLocalTypePasses(t *testing.T) {
	for _, name := range []string{"apfs", "hfs", "ufs", "msdos", "exfat", "tmpfs"} {
		if got := localfs.ClassifyDarwin(name).Class; got != localfs.Local {
			t.Errorf("darwin %s is local, got class %v", name, got)
		}
	}
	// ext4 xfs btrfs tmpfs overlayfs zfs f2fs ramfs
	for _, magic := range []uint32{0xef53, 0x58465342, 0x9123683e, 0x01021994, 0x794c7630, 0x2fc12fc1, 0xf2f52010, 0x858458f6} {
		if got := localfs.ClassifyLinux(magic).Class; got != localfs.Local {
			t.Errorf("linux magic 0x%x is local, got class %v", magic, got)
		}
	}
}

func TestAnUnknownTypeIsRefusedWithItsNumber(t *testing.T) {
	fs := localfs.ClassifyLinux(uint32(0x12345678))
	if fs.Class != localfs.Unknown || !strings.Contains(fs.Type, "0x12345678") {
		t.Fatalf("got %+v, want an unknown type carrying its statfs number", fs)
	}
	requireRefused(t, fs, "does not know to be local")
	if got := localfs.ClassifyDarwin("somefs"); got.Class != localfs.Unknown || got.Type != "somefs" {
		t.Fatalf("an unlisted darwin type must be unknown, got %+v", got)
	}
	requireRefused(t, localfs.ClassifyDarwin("somefs"), "does not know to be local")
	// The zero Filesystem, which a Detector returns when it knows nothing, is refused too.
	requireRefused(t, localfs.Filesystem{Type: "?"}, "does not know to be local")
}

// TestAClassIsTheTablesAndNothingElse is the property behind the fail-closed rule: over arbitrary
// numbers, a type is Local exactly when the local list names it and Network exactly when the
// network list does, so no unlisted number is ever taken as local.
func TestAClassIsTheTablesAndNothingElse(t *testing.T) {
	networkList := []uint32{0x6969, 0x517b, 0xfe534d42, 0xff534d42, 0xc36400, 0x5346414f, 0x73757245, 0x564c,
		0x1021997, 0x65735546, 0x0bd00bd0, 0x1161970, 0x7461636f, 0xaad7aaea, 0x19830326, 0x786f4256}
	localList := []uint32{0xef53, 0x58465342, 0x9123683e, 0x01021994, 0x858458f6, 0x958458f6, 0x794c7630, 0x2fc12fc1,
		0xf2f52010, 0x3153464a, 0x52654973, 0xca451a4e, 0x7366746e, 0x4d44, 0x2011bab0, 0xf15f, 0x24051905}
	want := map[uint32]localfs.Class{}
	for _, m := range networkList {
		want[m] = localfs.Network
	}
	for _, m := range localList {
		want[m] = localfs.Local
	}
	for m, class := range want {
		if got := localfs.ClassifyLinux(m).Class; got != class {
			t.Errorf("0x%x: class %v, want %v", m, got, class)
		}
	}
	r := rand.New(rand.NewPCG(1, 2))
	for range 20000 {
		m := r.Uint32()
		if got := localfs.ClassifyLinux(m).Class; got != want[m] { // want[m] is Unknown for an unlisted number
			t.Fatalf("0x%x: class %v, want %v", m, got, want[m])
		}
	}
}

// TestA32BitStatfsTypeIsTheSameTypeAsItsUnsignedNumber: on 32-bit Linux f_type is a signed int32, so
// CIFS (0xff534d42) arrives negative. It must still be recognised, never taken as unknown or local.
func TestA32BitStatfsTypeIsTheSameTypeAsItsUnsignedNumber(t *testing.T) {
	for _, magic := range []uint32{0xff534d42, 0xfe534d42, 0xaad7aaea, 0x9123683e, 0x794c7630, 0xef53} {
		signed := int32(magic) //nolint:gosec // The conversion is the point: the kernel's bit pattern read as the signed type 32-bit Linux declares.
		want := localfs.ClassifyLinux(magic)
		if got := localfs.ClassifyLinux(signed); got != want {
			t.Errorf("int32 %d (0x%x): %+v, want %+v", signed, magic, got, want)
		}
		if got := localfs.ClassifyLinux(int64(magic)); got != want {
			t.Errorf("int64 0x%x: %+v, want %+v", magic, got, want)
		}
	}
	if localfs.ClassifyLinux(int32(-11386306)).Class == localfs.Local { // an arbitrary negative number
		t.Error("a negative number was taken as local")
	}
}

func TestADirectoryThatDoesNotExistYetIsJudgedByItsNearestAncestor(t *testing.T) {
	dir := t.TempDir()
	detector := localfstest.Fake{Mounts: map[string]localfs.Filesystem{dir: local}}
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
	detector := localfstest.Fake{Mounts: map[string]localfs.Filesystem{real: nfs}}
	_, err = localfs.Resolve(filepath.Join(link, "state"), detector)
	var refusal *localfs.RefusedError
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
	local, err := localfs.Resolve(filepath.Join(link, "state"), localfstest.Fake{Mounts: map[string]localfs.Filesystem{root: nfs, real: local}})
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
	_, err := localfs.Resolve(dir, localfstest.Fake{Unreadable: map[string]bool{real: true}})
	if err == nil || !strings.Contains(err.Error(), localfs.EnvStateDir) {
		t.Fatalf("want a refusal naming %s, got %v", localfs.EnvStateDir, err)
	}
}

func TestTheDefaultDirectoryIsRefusedNotRelocated(t *testing.T) {
	home := t.TempDir()
	realHome, _ := filepath.EvalSymlinks(home)
	detector := localfstest.Fake{Mounts: map[string]localfs.Filesystem{realHome: nfs}}
	_, err := localfs.StateDir(noEnv, home, detector)
	var refusal *localfs.RefusedError
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
	}, home, localfstest.Fake{Mounts: map[string]localfs.Filesystem{realHome: nfs}})
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
	}, "/home/x", localfstest.Fake{})
	if err == nil || !strings.Contains(err.Error(), localfs.EnvStateDir) {
		t.Fatalf("want a rejection naming the variable, got %v", err)
	}
}

func TestAnEmptyHomeNeverMakesTheDefaultRelative(t *testing.T) {
	_, err := localfs.StateDir(noEnv, "", localfstest.Fake{})
	if err == nil || !strings.Contains(err.Error(), localfs.EnvStateDir) || !strings.Contains(err.Error(), "home") {
		t.Fatalf("want a refusal that names the home directory and %s, got %v", localfs.EnvStateDir, err)
	}
	// An absolute XDG_STATE_HOME needs no home, and the variable always wins.
	get := func(k string) string {
		if k == "XDG_STATE_HOME" {
			return t.TempDir()
		}
		return ""
	}
	if got, err := localfs.StateDir(get, "", localfstest.Fake{}); err != nil || !filepath.IsAbs(got.Path()) {
		t.Fatalf("an absolute XDG_STATE_HOME must stand without a home: %v %v", got, err)
	}
}
