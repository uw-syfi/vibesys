package wire_test

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"os"
	"path/filepath"
	"testing"
)

func TestSearchConformanceCorpus(t *testing.T) {
	files, err := filepath.Glob("../../../proto/vibesys/orchestrator/v1/conformance/search/*.json")
	if err != nil || len(files) != 3 {
		t.Fatalf("corpus: %v %v", files, err)
	}
	for _, file := range files {
		t.Run(filepath.Base(file), func(t *testing.T) {
			data, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			msg := new(pb.WatchRunsResponse)
			if err := protojson.Unmarshal(data, msg); err != nil {
				t.Fatal(err)
			}
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

func TestSearchCapsAreValidatedAtTheBoundary(t *testing.T) {
	data, err := os.ReadFile("../../../proto/vibesys/orchestrator/v1/conformance/search/live.json")
	if err != nil {
		t.Fatal(err)
	}
	frame := new(pb.WatchRunsResponse)
	if err := protojson.Unmarshal(data, frame); err != nil {
		t.Fatal(err)
	}
	for field, cap := range map[string]int{"jobs": 500, "results": 500, "judgments": 100, "promotions": 200, "plans": 200, "troubles": 500} {
		t.Run(field, func(t *testing.T) {
			s := proto.Clone(frame.GetSearch()).(*pb.SearchState)
			m := s.ProtoReflect()
			fd := m.Descriptor().Fields().ByName(protoreflect.Name(field))
			list := m.Mutable(fd).List()
			value := list.Get(0)
			for list.Len() < cap {
				list.Append(value)
			}
			if err := wire.Validate(s); err != nil {
				t.Fatal(err)
			}
			list.Append(value)
			if wire.Validate(s) == nil {
				t.Fatal("accepted over-cap list", field)
			}
		})
	}
}
