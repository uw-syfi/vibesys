package server

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// Watch runs one registered stream. The process group dies when this RPC's
// context ends, which is an explicit cancel or the operation cap. A caller that
// only drops a subscription keeps its own stream open, so this is not that path.
func (h *host) Watch(request *apiv1.WatchRequest, stream apiv1.HostService_WatchServer) error {
	dispatched, ok := hostoperations.DispatchHostWatch(request)
	if !ok {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostWatch.method is required"))
	}
	checkout, err := checkoutPath(request.GetCheckout())
	if err != nil {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()))
	}
	input, err := proto.Marshal(request)
	if err != nil {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, err.Error()))
	}
	if len(input) > hostcommand.MaxOutput {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "HostWatch request exceeds 1 MiB"))
	}
	callCtx, cancel := context.WithTimeout(stream.Context(), dispatched.Deadline)
	defer cancel()
	// A stream is bounded by its silence: FirstEvent before its first frame, then Progress
	// between two. The cap above is only the bound on a stream that never ends.
	guard := guardSilence(cancel, dispatched.FirstEvent, dispatched.Progress)
	defer guard.stop()
	sent := false
	var rejected *apiv1.WatchResponse
	err = h.config.HostRunner.Stream(callCtx, hostcommand.Command{
		Executable: filepath.Join(checkout, ".venv", "bin", "vibesys"),
		Args:       []string{"host", dispatched.CLI}, Input: input, StateRoot: h.config.Root,
	}, func(payload []byte) error {
		response := &apiv1.WatchResponse{}
		if unmarshalErr := proto.Unmarshal(payload, response); unmarshalErr != nil || response.Result == nil {
			rejected = watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "vibesys host "+dispatched.CLI+" returned invalid protobuf")
			return errors.New("invalid watch frame")
		}
		if response.GetFailure() == nil && !hostoperations.HostWatchResponseValid(request, response) {
			rejected = watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT, "vibesys host "+dispatched.CLI+" returned a different method or identity")
			return errors.New("unrelated watch frame")
		}
		if sendErr := stream.Send(response); sendErr != nil {
			return sendErr
		}
		sent = true
		guard.frame()
		return nil
	})
	if rejected != nil {
		return stream.Send(rejected)
	}
	// A stream that declares Progress is finite and ends with a frame of its own saying so, so
	// any other end is a failure the client must hear, after the frames it already holds.
	if sent && (dispatched.Progress == 0 || err == nil) {
		return nil
	}
	if silent := guard.silentFor(); silent > 0 {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED, silenceMessage("watch", dispatched.CLI, silent)))
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED, deadlineMessage("watch", dispatched.CLI, dispatched.Deadline)))
	}
	if errors.Is(err, context.Canceled) {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_CANCELLED, "host watch cancelled"))
	}
	if unsupportedStream(dispatched.CLI, err) {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED, unsupportedMessage(dispatched.CLI)))
	}
	if err != nil {
		return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL, fmt.Sprintf("vibesys host %s: %s", dispatched.CLI, err.Error())))
	}
	return stream.Send(watchFailure(hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL, "host watch ended before its first event"))
}

func watchFailure(category hostv1.FailureCategory, message string) *apiv1.WatchResponse {
	text := safeDiagnostic(message)
	if text == "" {
		text = "host watch failed"
	}
	return &apiv1.WatchResponse{Result: &apiv1.WatchResponse_Failure{Failure: &hostv1.OperationFailure{
		Category: category, Message: text,
	}}}
}
