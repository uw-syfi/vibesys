package protocol_test

import (
	"testing"
	"testing/quick"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/propcheck"
	"github.com/uw-syfi/vibesys/orchestrator/internal/protocol"
)

// SummaryProtocol is the first protocol whose host serves what the desktop app requires: WatchRuns with
// detail SUMMARY and its RunSummary frames, and GetRunHistory. A host of an earlier protocol ignores the
// detail and lacks the call, so a client that needs them must be refused by it.
const summaryProtocol = 2

func build(version, min uint32) *pb.BuildVersions {
	return &pb.BuildVersions{BinaryVersion: "test", ProtocolVersion: version, MinProtocolVersion: min}
}

// A client of this build facing a host of any earlier protocol is told the host is too old, so the app
// shows the typed refusal and not a stream that silently lacks what it asks for. The host decides this
// from the client's min_protocol_version alone, which is why a host that predates the summaries refuses it.
func TestAClientThatNeedsSummariesIsRefusedByAnyOlderHost(t *testing.T) {
	if protocol.Version < summaryProtocol || protocol.MinVersion < summaryProtocol {
		t.Fatalf("this build says protocol %d, min %d: it needs hosts of protocol %d", protocol.Version, protocol.MinVersion, summaryProtocol)
	}
	property := func(hostVersion uint8) bool {
		older := uint32(hostVersion) % summaryProtocol // protocols 0 and 1
		host := build(older, older)
		return protocol.Judge(host, protocol.Build("app")) == pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD
	}
	if err := quick.Check(property, propcheck.Config(t, nil)); err != nil {
		t.Fatal(err)
	}
}

// A client of an earlier protocol facing this build is told it is too old, and equal builds agree.
func TestTheHostAnswersEveryPairOfBuildsWithTheSideToUpdate(t *testing.T) {
	this := protocol.Build("host")
	for _, tc := range []struct {
		name   string
		client *pb.BuildVersions
		want   pb.Compatibility
	}{
		{"the same build", protocol.Build("app"), pb.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"a client of protocol 1", build(1, 1), pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD},
		{"a newer client that still speaks this one", build(protocol.Version+3, protocol.Version), pb.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"a newer client that dropped this one", build(protocol.Version+3, protocol.Version+1), pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD},
	} {
		if got := protocol.Judge(this, tc.client); got != tc.want {
			t.Errorf("%s: %v, want %v", tc.name, got, tc.want)
		}
	}
}
