package wire_test

import (
	"os"
	"path/filepath"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

const summaryCorpus = "../../../proto/vibesys/orchestrator/v1/conformance/summary/"

func readSummaryFrame(t *testing.T, name string) *pb.WatchRunsResponse {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(summaryCorpus, name))
	if err != nil {
		t.Fatal(err)
	}
	frame := new(pb.WatchRunsResponse)
	if err := protojson.Unmarshal(data, frame); err != nil {
		t.Fatal(err)
	}
	return frame
}

// The frames a client of run summaries reads pass the contract's own rules and survive the wire.
func TestSummaryConformanceCorpus(t *testing.T) {
	files, err := filepath.Glob(summaryCorpus + "*.json")
	if err != nil || len(files) != 2 {
		t.Fatalf("corpus: %v %v", files, err)
	}
	for _, file := range files {
		t.Run(filepath.Base(file), func(t *testing.T) {
			msg := readSummaryFrame(t, filepath.Base(file))
			if err := wire.Validate(msg); err != nil {
				t.Fatal(err)
			}
			raw, err := proto.Marshal(msg)
			if err != nil {
				t.Fatal(err)
			}
			decoded := new(pb.WatchRunsResponse)
			if err := proto.Unmarshal(raw, decoded); err != nil || !proto.Equal(msg, decoded) {
				t.Fatal("wire roundtrip", err)
			}
		})
	}
}

// A summary the host would never send is refused by the contract: a report that answered a check-in as the
// run's summary, a report of another run, more agents working than there are, and a figure that is not a
// number.
func TestASummaryTheHostWouldNeverSendIsRefused(t *testing.T) {
	for name, damage := range map[string]func(*pb.RunSummary){
		"a window report as the summary": func(s *pb.RunSummary) {
			s.Report.GetReport().Scope = pb.ReportScope_REPORT_SCOPE_WINDOW
		},
		"another run's report": func(s *pb.RunSummary) { s.Report.RunId = "run-2" },
		"a message that is not a report": func(s *pb.RunSummary) {
			s.Report.Body = &pb.Message_Text{Text: "hello"}
		},
		"more agents working than there are": func(s *pb.RunSummary) { s.AgentsWorking = s.AgentsTotal + 1 },
		"more of a type working than there are": func(s *pb.RunSummary) {
			s.Roles[0].Working = s.Roles[0].Total + 1
		},
		"an unnamed objective": func(s *pb.RunSummary) { s.Objectives[0].Name = "" },
	} {
		t.Run(name, func(t *testing.T) {
			frame := readSummaryFrame(t, "frame.json")
			if err := wire.Validate(frame); err != nil {
				t.Fatal(err)
			}
			damage(frame.GetSummary())
			if wire.Validate(frame) == nil {
				t.Fatal("accepted")
			}
		})
	}
}
