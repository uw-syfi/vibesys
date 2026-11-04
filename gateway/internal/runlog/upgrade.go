package runlog

import (
	"sort"

	searchv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/search/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// upgradeInputSkeleton applies the same semantic input upgrade as record streaming.
// Declarations retain the sequence where their measurement became known.
func upgradeInputSkeleton(state *storev1.Keyframe) bool {
	var records []*storev1.Record
	for _, version := range state.Epochs {
		records = append(records, &storev1.Record{Seq: version.Seq, Body: &storev1.Record_EpochStarted{EpochStarted: version.Epoch}})
	}
	for _, history := range state.Spans {
		for _, version := range history.Versions {
			records = append(records, &storev1.Record{Seq: version.Seq, Body: &storev1.Record_Span{Span: &storev1.SpanRecord{Span: version.Span}}})
		}
	}
	for _, history := range state.Metrics {
		for _, version := range history.Points {
			records = append(records, &storev1.Record{Seq: version.Seq, Body: &storev1.Record_Metric{Metric: version.Point}})
		}
	}
	sort.SliceStable(records, func(i, j int) bool { return records[i].Seq < records[j].Seq })
	upgrade := NewInputUpgrade(nil)
	changed := false
	for _, record := range records {
		changed = upgrade.apply(record) || changed
		if record.InputMeasurement != nil && upgrade.epoch != nil {
			state.Epochs = append(state.Epochs, &storev1.EpochVersion{Seq: record.Seq, Epoch: proto.Clone(upgrade.epoch).(*storev1.EpochStarted)})
		}
	}
	if changed {
		sort.SliceStable(state.Epochs, func(i, j int) bool { return state.Epochs[i].Seq < state.Epochs[j].Seq })
	}
	return changed
}

// InputUpgrade owns the minimal store context used by every semantic reader.
// Bodies, serial sequences and persisted bytes are preserved.
type InputUpgrade struct {
	epoch  *storev1.EpochStarted
	inputs map[string]bool
}

func NewInputUpgrade(state *storev1.Keyframe) *InputUpgrade {
	u := &InputUpgrade{inputs: map[string]bool{}}
	if state == nil {
		return u
	}
	upgradeInputSkeleton(state)
	if len(state.Epochs) > 0 && state.Epochs[len(state.Epochs)-1].GetEpoch() != nil {
		u.epoch = proto.Clone(state.Epochs[len(state.Epochs)-1].Epoch).(*storev1.EpochStarted)
	}
	for _, history := range state.Spans {
		if len(history.Versions) > 0 {
			u.span(history.Versions[len(history.Versions)-1].Span)
		}
	}
	return u
}

// Record returns original bytes unless a retained record needs a declaration fact.
func (u *InputUpgrade) Record(payload []byte) ([]byte, error) {
	record := &storev1.Record{}
	if err := proto.Unmarshal(upgradeMetadata(payload), record); err != nil {
		return nil, err
	}
	if !u.apply(record) {
		return payload, nil
	}
	return proto.Marshal(record)
}

func (u *InputUpgrade) apply(record *storev1.Record) bool {
	changed := false
	switch body := record.Body.(type) {
	case *storev1.Record_EpochStarted:
		changed = u.declaration(body.EpochStarted)
	case *storev1.Record_DeclarationAmended:
		changed = u.declaration(body.DeclarationAmended)
	case *storev1.Record_Span:
		u.span(body.Span.GetSpan())
	case *storev1.Record_Metric:
		run := u.epoch.GetRun()
		point := body.Metric
		if run != nil && point != nil && run.InputMeasurement == nil && len(run.Objectives) > 0 && point.Series == run.Objectives[0].Series && (point.GetSpan() == "" || u.inputs[point.GetSpan()]) {
			record.InputMeasurement = &storev1.InputMeasurement{State: &storev1.InputMeasurement_Measured{Measured: &storev1.MeasuredInput{Series: point.Series, Value: point.Value, Span: point.GetSpan()}}}
			changed = true
		}
	}
	if record.InputMeasurement != nil && u.epoch.GetRun() != nil {
		u.epoch.Run.InputMeasurement = proto.Clone(record.InputMeasurement).(*storev1.InputMeasurement)
	}
	return changed
}

func (u *InputUpgrade) declaration(epoch *storev1.EpochStarted) bool {
	if epoch == nil {
		u.epoch = nil
		return false
	}
	changed := false
	if epoch.GetRun() != nil && epoch.Run.InputMeasurement == nil && sameInput(u.epoch, epoch) && u.epoch.GetRun().GetInputMeasurement() != nil {
		epoch.Run.InputMeasurement = proto.Clone(u.epoch.Run.InputMeasurement).(*storev1.InputMeasurement)
		changed = true
	}
	u.epoch = proto.Clone(epoch).(*storev1.EpochStarted)
	return changed
}

func (u *InputUpgrade) span(span *storev1.Span) {
	if span == nil {
		return
	}
	attrs := &searchv1.GateAttrs{}
	u.inputs[span.Id] = span.GetLabel().GetKind() == "gate" && span.GetLabel().GetAttrs().GetTypeUrl() == "type.googleapis.com/vibesys.search.v1.GateAttrs" && proto.Unmarshal(span.GetLabel().GetAttrs().GetValue(), attrs) == nil && attrs.Subject == searchv1.GateSubject_GATE_SUBJECT_INPUT
}

// Measurement is the latest declaration's canonical fact after the read prefix.
func (u *InputUpgrade) Measurement() *storev1.InputMeasurement {
	return u.epoch.GetRun().GetInputMeasurement()
}

// Irrelevant bodies remain opaque, including bodies added by newer writers.
func upgradeMetadata(payload []byte) []byte    { return filteredMetadata(payload, false) }
func projectionMetadata(payload []byte) []byte { return filteredMetadata(payload, true) }
func filteredMetadata(payload []byte, lifecycle bool) []byte {
	fields := (&storev1.Record{}).ProtoReflect().Descriptor().Oneofs().ByName("body").Fields()
	var filtered []byte
	for len(payload) > 0 {
		number, kind, width := protowire.ConsumeTag(payload)
		if width < 0 {
			return payload
		}
		n := protowire.ConsumeFieldValue(number, kind, payload[width:])
		if n < 0 {
			return payload
		}
		field := fields.ByNumber(number)
		if field != nil && kind == protowire.BytesType && field.Name() != "epoch_started" && field.Name() != "declaration_amended" && field.Name() != "span" && field.Name() != "metric" && !(lifecycle && field.Name() == "lifecycle") {
			filtered = protowire.AppendTag(filtered, number, kind)
			filtered = protowire.AppendBytes(filtered, nil)
		} else {
			filtered = append(filtered, payload[:width+n]...)
		}
		payload = payload[width+n:]
	}
	return filtered
}

func sameInput(left, right *storev1.EpochStarted) bool {
	previous, current := left.GetRun(), right.GetRun()
	return previous != nil && current != nil && previous.Input == current.Input && len(previous.Objectives) > 0 && len(current.Objectives) > 0 && previous.Objectives[0].Series == current.Objectives[0].Series
}

// UpgradeSkeleton restores input and performance facts without rewriting retained bytes.
func UpgradeSkeleton(state *storev1.Keyframe) (bool, error) {
	changed := upgradeInputSkeleton(state)
	if state.Head == nil || !state.Head.PerformanceStated || state.Head.GetCommittedSeq() != state.ThroughSeq {
		head, err := ProjectHead(state)
		if err != nil {
			return false, err
		}
		if head != nil {
			state.Head = head
			changed = true
		}
	}
	return changed, nil
}
