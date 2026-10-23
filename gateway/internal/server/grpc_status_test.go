package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"testing"

	"github.com/uw-syfi/vibesys/gateway/internal/runindex"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestSharedGrpcStatuses(t *testing.T) {
	// test-isolation: exercise the gateway's error translation without starting Serve's sockets or goroutines.
	data, err := os.ReadFile("../../../proto/testdata/grpc_status/statuses.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus []struct {
		Name, Source, Error, Message, GrpcMessage string
		Code                                      uint32
	}
	if err := json.Unmarshal(data, &corpus); err != nil {
		t.Fatal(err)
	}
	seen := map[codes.Code]bool{}
	for _, row := range corpus {
		t.Run(row.Name, func(t *testing.T) {
			var input error
			switch row.Source {
			case "status":
				input = status.Error(codes.Code(row.Code), row.Error)
			case "missing":
				input = os.ErrNotExist
			case "permission":
				input = os.ErrPermission
			case "cancelled":
				input = context.Canceled
			case "deadline":
				input = context.DeadlineExceeded
			case "invalid-id":
				input = runindex.ErrInvalidID
			case "corruption":
				input = &runlog.Corruption{Offset: -1, Reason: row.Error}
			case "ordinary":
				input = errors.New(row.Error)
			default:
				t.Fatalf("unknown source %q", row.Source)
			}
			actual := status.Convert(rpcError(input))
			if uint32(actual.Code()) != row.Code || actual.Message() != row.Message {
				t.Fatalf("got %d %q, want %d %q", actual.Code(), actual.Message(), row.Code, row.Message)
			}
			decoded, err := url.PathUnescape(row.GrpcMessage)
			if err != nil || decoded != actual.Message() {
				t.Fatalf("wire message %q: %v", decoded, err)
			}
			seen[actual.Code()] = true
		})
	}
	if len(seen) != 17 {
		t.Fatalf("corpus covers %d statuses, want all 17", len(seen))
	}
}
