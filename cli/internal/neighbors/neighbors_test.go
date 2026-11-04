package neighbors_test

import (
	"reflect"
	"testing"
	"testing/fstest"

	"github.com/uw-syfi/vibesys/cli/internal/neighbors"
)

func TestTheDaemonsOfTheMachineAreFound(t *testing.T) {
	proc := fstest.MapFS{
		// Two users' daemons, this one, and others that are not daemons.
		"12/cmdline":  {Data: []byte("vibesysd\x00daemon\x00run\x00")},
		"12/status":   {Data: []byte("Name:\tvibesys-cli\nUid:\t1001\t1001\t1001\t1001\n")},
		"7/cmdline":   {Data: []byte("/usr/local/bin/vibesysd\x00daemon\x00run\x00")},
		"7/status":    {Data: []byte("Uid:\t1000\t1000\t1000\t1000\n")},
		"40/cmdline":  {Data: []byte("vibesysd\x00daemon\x00run\x00")},
		"40/status":   {Data: []byte("Uid:\t1000\t1000\t1000\t1000\n")},
		"41/cmdline":  {Data: []byte("vibesys-cli\x00jobs\x00")},
		"41/status":   {Data: []byte("Uid:\t1000\n")},
		"42/cmdline":  {Data: []byte("bash\x00-c\x00vibesysd\x00")},
		"42/status":   {Data: []byte("Uid:\t1000\n")},
		"43/cmdline":  {Data: []byte("")},
		"self/status": {Data: []byte("Uid:\t1000\n")},
		// A daemon whose status cannot be read is left out.
		"50/cmdline": {Data: []byte("vibesysd\x00")},
	}
	got := neighbors.Find(proc, "vibesysd", 40)
	want := []neighbors.Process{{PID: 7, UID: 1000}, {PID: 12, UID: 1001}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("found %+v, want %+v", got, want)
	}
}
