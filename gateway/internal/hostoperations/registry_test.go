package hostoperations_test

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"

	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

func TestSharedRequestCorpus(t *testing.T) {
	payload, err := os.ReadFile("../../../proto/testdata/operations/requests.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name    string
		ID      uint32
		Payload string
		Valid   bool
	}
	if err := json.Unmarshal(payload, &cases); err != nil {
		t.Fatal(err)
	}
	for _, test := range cases {
		t.Run(test.Name, func(t *testing.T) {
			operation, err := hostoperations.Lookup(test.ID)
			if err == nil {
				bytes, decodeErr := hex.DecodeString(test.Payload)
				if decodeErr != nil {
					t.Fatal(decodeErr)
				}
				_, err = operation.Decode(bytes)
			}
			if (err == nil) != test.Valid {
				t.Fatalf("valid=%v, error=%v", test.Valid, err)
			}
		})
	}
}

func TestDispatchChecksPairingAndCorrelation(t *testing.T) {
	request, _ := proto.Marshal(&hostv1.ListTasksRequest{RequestId: "request-a", Project: "/project"})
	invoke := func(ctx context.Context, op hostoperations.Operation, input proto.Message) (proto.Message, error) {
		if _, ok := ctx.Deadline(); !ok {
			t.Fatal("unbounded executor")
		}
		if op.ID != 10 || op.Cap+op.Grace != 15000000000 {
			t.Fatal("wrong descriptor budget")
		}
		if input.(*hostv1.ListTasksRequest).Project != "/project" {
			t.Fatal("wrong request")
		}
		return &hostv1.ListTasksResponse{RequestId: "request-a", Tasks: &hostv1.TaskList{Version: 1, ProjectRoot: "/project"}}, nil
	}
	if _, err := hostoperations.Execute(context.Background(), 10, request, invoke); err != nil {
		t.Fatal(err)
	}
	op, _ := hostoperations.Lookup(10)
	if err := op.ValidateResponse(&hostv1.ListTasksRequest{RequestId: "request-a", Project: "/project"}, &hostv1.ListTasksResponse{RequestId: "other", Tasks: &hostv1.TaskList{Version: 1}}); err == nil {
		t.Fatal("wrong response identity accepted")
	}
	if err := op.ValidateResponse(&hostv1.ListTasksRequest{RequestId: "request-a", Project: "/project"}, &hostv1.ListComputeResponse{}); err == nil {
		t.Fatal("wrong method accepted")
	}
}

func TestLookupCannotMutateTheRegistry(t *testing.T) {
	first, _ := hostoperations.Lookup(10)
	first.Correlations[0][0][0] = "other"
	second, _ := hostoperations.Lookup(10)
	if second.Correlations[0][0][0] != "request_id" {
		t.Fatal("shared mutable registry")
	}
}
