package client_test

import (
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"google.golang.org/protobuf/proto"
)

func TestDecodeEveryWatchRunsKind(t *testing.T) {
	descriptor := (&pb.WatchRunsResponse{}).ProtoReflect().Descriptor().Oneofs().ByName("body")
	for i := 0; i < descriptor.Fields().Len(); i++ {
		field := descriptor.Fields().Get(i)
		t.Run(string(field.Name()), func(t *testing.T) {
			frame := &pb.WatchRunsResponse{}
			reflected := frame.ProtoReflect()
			reflected.Set(field, reflected.NewField(field))
			snapshot, event, search := client.DecodeWatchRuns(frame)
			var decoded proto.Message
			switch field.Name() {
			case "snapshot":
				decoded = snapshot
			case "event":
				decoded = event
			case "search":
				decoded = search
			default:
				t.Fatalf("add a fixture assertion for new kind %s", field.Name())
			}
			if !proto.Equal(decoded, reflected.Get(field).Message().Interface()) {
				t.Fatalf("decoder lost %s", field.Name())
			}
		})
	}
}

func TestDecodeMissingWatchRunsKindPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("missing body must fail loudly")
		}
	}()
	client.DecodeWatchRuns(&pb.WatchRunsResponse{})
}
