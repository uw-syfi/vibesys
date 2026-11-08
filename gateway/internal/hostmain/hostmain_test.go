package hostmain

import (
	"reflect"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
)

// TestStayedLogNamesTheDecisiveReason pins that the "it stays" operator log
// reports the real reason a running host of another API kept the socket, rather
// than always claiming live runs. DialForAPI keeps such a host when it is a
// newer release, when it has live runs, when it has other clients connected, or
// when it declined to stop; each must read back as itself, and live_runs must
// not be announced when it is zero.
func TestStayedLogNamesTheDecisiveReason(t *testing.T) {
	identity := func(relation pb.ReleaseRelation, liveRuns, clients uint32) *hostconfig.IdentifyResponse {
		return &pb.IdentifyResponse{Release: "7.7.7", Relation: relation, LiveRuns: liveRuns, ConnectedClients: clients}
	}
	for _, c := range []struct {
		name    string
		id      *hostconfig.IdentifyResponse
		message string
		attrs   []any
	}{
		{
			"a newer release stays as newer even with runs and clients",
			identity(pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER, 3, 4),
			"the running host speaks a newer release, so it stays",
			[]any{"release", "7.7.7"},
		},
		{
			"live runs are reported as live runs",
			identity(pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, 2, 0),
			"the running host speaks another API and has live runs, so it stays",
			[]any{"live_runs", uint32(2), "release", "7.7.7"},
		},
		{
			"connected clients without live runs are reported as clients, not runs",
			identity(pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, 0, 5),
			"the running host speaks another API and has other connected clients, so it stays",
			[]any{"connected_clients", uint32(5), "release", "7.7.7"},
		},
		{
			"a host that declined to stop with no runs or clients is not claimed to have runs",
			identity(pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER, 0, 0),
			"the running host speaks another API and declined to stop, so it stays",
			[]any{"release", "7.7.7"},
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			message, attrs := stayedLog(c.id)
			if message != c.message {
				t.Errorf("message = %q, want %q", message, c.message)
			}
			if !reflect.DeepEqual(attrs, c.attrs) {
				t.Errorf("attrs = %v, want %v", attrs, c.attrs)
			}
		})
	}
}
