package runtimedir_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// A drain the directory keeps is what the next read returns, a second one replaces it, removing it
// reads as no drain, and the file never lies half written beside another.
func TestADrainIsKeptUntilItIsRemoved(t *testing.T) {
	d, err := runtimedir.Open(filepath.Join(shortDir(t), "rt"), os.Getuid(), fakeFS{})
	if err != nil {
		t.Fatal(err)
	}
	if got, err := d.ReadDrain(); got != nil || err != nil {
		t.Fatalf("a new directory reads as %v, %v; want no drain", got, err)
	}
	since := timestamppb.New(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC))
	for _, drain := range []*cliv1.DaemonDrain{
		{Agent: "lead", Reason: "a new build", Since: since},
		{Since: since},
	} {
		if err := d.WriteDrain(drain); err != nil {
			t.Fatal(err)
		}
		got, err := d.ReadDrain()
		if err != nil || !proto.Equal(got, drain) {
			t.Fatalf("ReadDrain = %v, %v; want %v", got, err, drain)
		}
	}
	if err := d.WriteDrain(nil); err != nil {
		t.Fatal(err)
	}
	if got, err := d.ReadDrain(); got != nil || err != nil {
		t.Fatalf("after the drain was removed: %v, %v; want no drain", got, err)
	}
	if err := d.WriteDrain(nil); err != nil {
		t.Fatalf("removing no drain: %v", err)
	}
	entries, _ := os.ReadDir(d.Path())
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".drain") {
			t.Errorf("a temporary file is left: %s", e.Name())
		}
	}
}

// A drain file that does not decode, or a drain with no time it began, is an error naming the file
// and never a daemon that does not drain; a drain with no time is not written.
func TestADamagedDrainIsAnErrorThatNamesItsFile(t *testing.T) {
	d, err := runtimedir.Open(filepath.Join(shortDir(t), "rt"), os.Getuid(), fakeFS{})
	if err != nil {
		t.Fatal(err)
	}
	for _, content := range []string{"{", `{"agent":"lead"}`, `{"unknown":1}`} {
		if err := os.WriteFile(d.DrainFile(), []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		got, err := d.ReadDrain()
		if err == nil || !strings.Contains(err.Error(), d.DrainFile()) {
			t.Errorf("%q reads as %v, %v; want an error that names %s", content, got, err, d.DrainFile())
		}
	}
	if err := d.WriteDrain(&cliv1.DaemonDrain{Agent: "lead"}); err == nil {
		t.Errorf("a drain with no time it began was written")
	}
}
