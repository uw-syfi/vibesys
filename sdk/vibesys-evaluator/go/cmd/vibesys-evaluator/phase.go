package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
)

// The `phase` group: one microservices load phase for a language SDK's phase runner
// (proto/vibesys/evaluator/v1/microservices.proto). `phase pod` runs where the phase pod runs and
// streams PhaseEvents; `phase judge` runs in the orchestrator on the files the pod left.
func init() {
	register("phase", "pod", phasePod)
	register("phase", "judge", typed(phaseJudge))
}

// phasePod runs one phase here: {"event": PhaseEvent} lines while loadgen runs, then the
// PhasePodResponse. The first SIGTERM, SIGINT or SIGHUP stops loadgen.
func phasePod(rw IO) error {
	in, err := io.ReadAll(rw.Stdin)
	if err != nil {
		return err
	}
	req := &pb.PhasePodRequest{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(in, req); err != nil {
		return fail("bad_request", "the request is not a valid message: %v", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	defer stop()
	var mu sync.Mutex
	var writeErr error
	emit := func(stage, message string) {
		mu.Lock()
		defer mu.Unlock()
		line, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(
			&pb.PhaseEvent{Stage: stage, Message: message, UnixNanos: time.Now().UnixNano()})
		if err == nil {
			_, err = fmt.Fprintf(rw.Stdout, "{\"event\":%s}\n", line)
		}
		if err != nil && writeErr == nil {
			writeErr = err
		}
	}
	pod := phase.Pod{
		WorkDir: req.GetWorkDir(), Target: req.GetTarget(), Generator: req.GetGenerator(),
		DatasetFile: req.GetDatasetFile(), MinSustainedRatio: req.GetMinSustainedRatio(),
		Loadgen: req.GetLoadgen(), Stderr: rw.Stderr, Event: emit,
	}
	output, err := pod.Run(ctx)
	var podErr *phase.PodError
	switch {
	case errors.As(err, &podErr):
		return fail("phase", "%s", podErr.Msg)
	case ctx.Err() != nil:
		return fail("cancelled", "the phase was stopped by a signal")
	case err != nil:
		return err
	}
	if writeErr != nil {
		return writeErr
	}
	s := output.GetSummary()
	return writeMessage(rw.Stdout, &pb.PhasePodResponse{
		Sent: s.GetSent(), Completed: s.GetCompleted(), Errors: s.GetErrors(),
		AchievedRate: s.GetAchievedRate(), LoadgenSecs: output.GetLoadgenSecs(),
	})
}

func writeMessage(out io.Writer, message proto.Message) error {
	encoded, err := protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true}.Marshal(message)
	if err != nil {
		return err
	}
	_, err = out.Write(append(encoded, '\n'))
	return err
}

// phaseJudge says whether the phase in phase_dir is a measurement.
func phaseJudge(req *pb.PhaseJudgeRequest) (*pb.PhaseJudgeResponse, error) {
	in := req.GetInput()
	if in == nil {
		return nil, fail("bad_request", "the request has no input (the PhaseInput the phase ran)")
	}
	v := req.GetValidity()
	validity := evaluator.Validity{
		MaxErrorRate: v.GetMaxErrorRate(), MinSustainedRatio: v.GetMinSustainedRatio(),
		MinOpsPerOperation: int(v.GetMinOpsPerOperation()),
	}
	if err := validity.Validate(); err != nil {
		return nil, fail("bad_request", "%v", err)
	}
	output, err := phase.ReadOutput(req.GetPhaseDir())
	if err != nil {
		return &pb.PhaseJudgeResponse{Failure: fmt.Sprintf("phase %d (%s) left no usable output: %v",
			in.GetPhaseIndex(), phase.Describe(in), err)}, nil
	}
	verdict, err := phase.Judge(output, validity)
	if err != nil {
		return &pb.PhaseJudgeResponse{Failure: fmt.Sprintf("phase %d: %v", in.GetPhaseIndex(), err)}, nil
	}
	resp := &pb.PhaseJudgeResponse{Log: phase.Log(in, output, verdict)}
	if verdict.Failure != "" {
		resp.Failure = phase.Failure(in, verdict.Failure)
		return resp, nil
	}
	m := verdict.Measurement
	resp.Measurement = &pb.PhaseMeasurement{OpsPerSec: m.OpsPerSec, P50Ms: m.P50Ms, P99Ms: m.P99Ms}
	return resp, nil
}
