package lifecycle

import (
	"os"
	"testing"
)

// The record in the lock file names the holder only while the lock is held. A record left by a
// process that has since exited names nobody, whatever it says, so a number the system has given to
// another program is never taken for the host.
func TestTheLockRecordIsReadOnlyWhileTheLockIsHeld(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "gw-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	if _, held, err := Holder(root); err != nil || held {
		t.Fatalf("no host has run: held %v, %v", held, err)
	}
	endpoint, err := Listen(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := endpoint.held.Record([]byte("first, a long record")); err != nil {
		t.Fatal(err)
	}
	if err := endpoint.held.Record([]byte("second")); err != nil {
		t.Fatal(err)
	}
	record, held, err := Holder(root)
	if err != nil || !held || string(record) != "second" {
		t.Fatalf("while held: %q, held %v, %v; want the second record, whole", record, held, err)
	}
	endpoint.Close()
	if record, held, err := Holder(root); err != nil || held || record != nil {
		t.Fatalf("after the host exited: %q, held %v, %v; want nobody", record, held, err)
	}
	// A new host can take the lock the probe just released.
	again, err := Listen(root)
	if err != nil {
		t.Fatalf("the probe left the lock held: %v", err)
	}
	again.Close()
}
