package main

import (
	"errors"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

func init() {
	register("records", "write", typed(recordsWrite))
}

func direction(d pb.Direction) evaluator.Direction {
	switch d {
	case pb.Direction_DIRECTION_MAX:
		return evaluator.Max
	case pb.Direction_DIRECTION_MIN:
		return evaluator.Min
	}
	return ""
}

// recordsWrite appends one record to the stream file, enforcing the protocol against what the
// file already holds.
func recordsWrite(req *pb.WriteRecordRequest) (*pb.WriteRecordResponse, error) {
	var s *evaluator.Stream
	if req.GetFresh() {
		s = evaluator.NewStream(req.GetPath())
	} else {
		var err error
		if s, err = evaluator.OpenStream(req.GetPath()); err != nil {
			return nil, fail("io", "%v", err)
		}
	}
	defer s.Close()
	var err error
	switch r := req.GetRecord().(type) {
	case *pb.WriteRecordRequest_Hello:
		var metrics []evaluator.NamedMetric
		for _, m := range r.Hello.GetMetrics() {
			metrics = append(metrics, evaluator.NamedMetric{Name: m.GetName(), Metric: evaluator.Metric{Unit: m.GetUnit(), Direction: direction(m.GetDirection())}})
		}
		err = s.Hello(metrics)
	case *pb.WriteRecordRequest_Result:
		values := map[string]float64{}
		for _, v := range r.Result.GetValues() {
			values[v.GetName()] = v.GetValue()
		}
		err = s.Result(values)
	case *pb.WriteRecordRequest_Error:
		var partial *evaluator.Partial
		if p := r.Error.Partial; p != nil {
			partial = &evaluator.Partial{Name: p.GetName(), Value: p.GetValue(), Direction: direction(p.GetDirection()), Unit: p.GetUnit()}
		}
		err = s.Error(r.Error.GetMessage(), partial)
	default:
		return nil, fail("bad_request", "write_record: set hello, result or error")
	}
	if err != nil {
		var re *evaluator.RecordError
		if errors.As(err, &re) {
			return nil, fail("record", "%s", re.Msg)
		}
		return nil, fail("io", "%v", err)
	}
	return &pb.WriteRecordResponse{Outcome: string(s.Outcome())}, nil
}
