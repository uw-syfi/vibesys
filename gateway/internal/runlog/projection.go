package runlog

import (
	"fmt"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"strings"
)

// Projection folds only metadata needed by the store's retained-facts upgrade.
// Product bodies outside that metadata remain opaque, like the input upgrade.
type Projection struct {
	State *storev1.Keyframe
	input *InputUpgrade
}

func NewProjection() *Projection { return NewProjectionFrom(nil) }

// NewProjectionFrom owns a copy of a checkpoint and folds only its subsequent records.
func NewProjectionFrom(seed *storev1.Keyframe) *Projection {
	state := &storev1.Keyframe{Spans: map[string]*storev1.SpanHistory{}}
	if seed != nil {
		state = proto.Clone(seed).(*storev1.Keyframe)
		if state.Spans == nil {
			state.Spans = map[string]*storev1.SpanHistory{}
		}
	}
	return &Projection{State: state, input: NewInputUpgrade(state)}
}
func (p *Projection) Record(payload []byte) error {
	record := &storev1.Record{}
	if err := proto.Unmarshal(projectionMetadata(payload), record); err != nil {
		return &Corruption{Offset: -1, Reason: err.Error()}
	}
	p.input.apply(record)
	state := p.State
	state.ThroughSeq = record.Seq
	switch body := record.Body.(type) {
	case *storev1.Record_EpochStarted:
		state.Epochs = append(state.Epochs, &storev1.EpochVersion{Seq: record.Seq, Epoch: body.EpochStarted})
	case *storev1.Record_DeclarationAmended:
		state.Epochs = append(state.Epochs, &storev1.EpochVersion{Seq: record.Seq, Epoch: body.DeclarationAmended})
	case *storev1.Record_Span:
		if span := body.Span.GetSpan(); span != nil {
			h := state.Spans[span.Id]
			if h == nil {
				h = &storev1.SpanHistory{}
				state.Spans[span.Id] = h
			}
			h.Versions = append(h.Versions, &storev1.SpanVersion{Seq: record.Seq, Span: span})
		}
	case *storev1.Record_Metric:
		var h *storev1.MetricHistory
		for _, each := range state.Metrics {
			if each.Series == body.Metric.Series {
				h = each
				break
			}
		}
		if h == nil {
			h = &storev1.MetricHistory{Series: body.Metric.Series}
			state.Metrics = append(state.Metrics, h)
		}
		h.Points = append(h.Points, &storev1.MetricVersion{Seq: record.Seq, Point: body.Metric})
	case *storev1.Record_Lifecycle:
		state.Lifecycles = append(state.Lifecycles, &storev1.LifecycleVersion{Seq: record.Seq, Lifecycle: body.Lifecycle})
	}
	if record.InputMeasurement != nil && p.input.epoch != nil {
		state.Epochs = append(state.Epochs, &storev1.EpochVersion{Seq: record.Seq, Epoch: proto.Clone(p.input.epoch).(*storev1.EpochStarted)})
	}
	return nil
}

// ProjectHead is the pure core projection used by all Go retained-store reads.
// Existing writer-stated heads remain authoritative at the observation boundary.
func ProjectHead(state *storev1.Keyframe) (*storev1.RunHead, error) {
	var epoch *storev1.EpochStarted
	var number uint32
	var start uint64
	for _, v := range state.Epochs {
		if v.GetEpoch().GetRun() != nil {
			epoch = v.Epoch
		}
		if n := v.GetEpoch().GetEpoch(); n > number {
			number, start = n, v.Seq
		}
	}
	if epoch == nil {
		return nil, nil
	}
	head := &storev1.RunHead{Strategy: epoch.Run.Strategy, Epoch: number, CommittedSeq: proto.Uint64(state.ThroughSeq), SpanCounts: map[string]uint64{}, PerformanceStated: true}
	for _, v := range state.Lifecycles {
		if v.Seq >= start {
			head.Lifecycle = proto.Clone(v.Lifecycle).(*storev1.Lifecycle)
		}
	}
	head.AwaitingLifecycle = head.Lifecycle == nil
	if ended := head.GetLifecycle().GetEnded(); ended != nil {
		value := ended.Outcome.Descriptor().Values().ByNumber(protoreflect.EnumNumber(ended.Outcome))
		if value == nil || !proto.HasExtension(value.Options(), storev1.E_OutcomePresentation) {
			return nil, fmt.Errorf("ended.outcome: unknown outcome %d", ended.Outcome)
		}
		ended.Presentation = proto.Clone(proto.GetExtension(value.Options(), storev1.E_OutcomePresentation).(*storev1.RunOutcomePresentation)).(*storev1.RunOutcomePresentation)
	}
	head.NeedsUser = head.GetLifecycle().GetRunning().GetHold().GetOperator() != nil
	var err error
	head.Performance, err = projectPerformance(state, epoch)
	if err != nil {
		return nil, err
	}
	for _, history := range state.Spans {
		if len(history.Versions) == 0 {
			continue
		}
		span := history.Versions[len(history.Versions)-1].Span
		label := span.GetLabel()
		if label == nil {
			continue
		}
		head.SpanCounts[label.Kind]++
		if attrs := label.Attrs; attrs != nil && strings.HasSuffix(attrs.TypeUrl, "/vibesys.store.v1.AgentTurnAttrs") {
			turn := &storev1.AgentTurnAttrs{}
			if err := proto.Unmarshal(attrs.Value, turn); err != nil {
				return nil, err
			}
			if turn.Usage != nil {
				head.Usage = addUsage(head.Usage, turn.Usage)
			}
		}
	}
	if len(epoch.Run.Objectives) > 0 {
		objective := epoch.Run.Objectives[0]
		var series *storev1.MetricSeriesDecl
		for _, d := range epoch.Series {
			if d.Name == objective.Series {
				series = d
				break
			}
		}
		if series == nil {
			return nil, fmt.Errorf("objective series %q is absent", objective.Series)
		}
		head.Objective = &storev1.HeadlineObjective{Series: proto.Clone(series).(*storev1.MetricSeriesDecl), InputStated: true, InputValue: head.Performance[0].InputValue}
		if head.Performance[0].Refusal == nil {
			head.Objective.BestValue = head.Performance[0].BestValue
		}
		for _, h := range state.Metrics {
			if h.Series == objective.Series {
				for _, v := range h.Points {
					if v.Point != nil {
						head.ChartPoints = append(head.ChartPoints, proto.Clone(v.Point).(*storev1.MetricPoint))
					}
				}
				break
			}
		}
		if len(head.ChartPoints) > 256 {
			head.ChartPoints = head.ChartPoints[len(head.ChartPoints)-256:]
		}
	}
	return head, nil
}
func addUsage(total, usage *storev1.TokenUsage) *storev1.TokenUsage {
	if total == nil {
		total = &storev1.TokenUsage{}
	}
	dst := total.ProtoReflect()
	usage.ProtoReflect().Range(func(f protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if f.Kind() == protoreflect.DoubleKind {
			dst.Set(f, protoreflect.ValueOfFloat64(dst.Get(f).Float()+v.Float()))
		} else {
			dst.Set(f, protoreflect.ValueOfUint64(dst.Get(f).Uint()+v.Uint()))
		}
		return true
	})
	return total
}
