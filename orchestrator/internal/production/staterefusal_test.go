package production

import (
	"errors"
	"fmt"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/localfs"
)

// A refused state directory is typed by what is wrong with its place, so a client names the setting
// that fixes it, and a refusal about something else stays unspecified.
func TestStateRefusalNamesWhatIsWrongWithThePlace(t *testing.T) {
	refusal := func(class localfs.Class, fsType string) error {
		return &localfs.RefusedError{Path: "/home/u/state", Resolved: "/home/u/state", Filesystem: localfs.Filesystem{Type: fsType, Class: class}}
	}
	cases := []struct {
		name string
		err  error
		want pb.StateRefusal
	}{
		{"nfs", refusal(localfs.Network, "nfs"), pb.StateRefusal_STATE_REFUSAL_NETWORK_FILESYSTEM},
		{"smb", refusal(localfs.Network, "smb2"), pb.StateRefusal_STATE_REFUSAL_NETWORK_FILESYSTEM},
		{"a type nobody listed", refusal(localfs.Unknown, "0x1234"), pb.StateRefusal_STATE_REFUSAL_UNKNOWN_FILESYSTEM},
		{"wrapped", fmt.Errorf("opening: %w", refusal(localfs.Network, "nfs")), pb.StateRefusal_STATE_REFUSAL_NETWORK_FILESYSTEM},
		{"another cause", errors.New("the state directory cannot be written"), pb.StateRefusal_STATE_REFUSAL_UNSPECIFIED},
	}
	for _, c := range cases {
		if got := StateRefusalOf(c.err); got != c.want {
			t.Errorf("%s: %v, want %v", c.name, got, c.want)
		}
	}
}
