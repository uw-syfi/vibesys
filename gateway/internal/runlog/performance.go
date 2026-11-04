package runlog

import (
	searchv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/search/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
	"sort"
)

// performanceContext is the retained-store counterpart of the Python core projector.
// Generated cross-language histories pin every decision and standing-value transition.
type performanceContext struct {
	spans       map[string]*storev1.Span
	owners      map[string]string
	groups      map[string][]*storev1.Span
	order       []string
	inputs      map[string]bool
	comparisons map[string]searchv1.ComparisonVerdict
}

func performanceSpans(state *storev1.Keyframe) (*performanceContext, error) {
	c := &performanceContext{spans: map[string]*storev1.Span{}, owners: map[string]string{}, groups: map[string][]*storev1.Span{}, inputs: map[string]bool{}, comparisons: map[string]searchv1.ComparisonVerdict{}}
	type entry struct {
		first uint64
		span  *storev1.Span
	}
	var ordered []entry
	for _, h := range state.Spans {
		if len(h.Versions) > 0 && h.Versions[len(h.Versions)-1].Span != nil {
			ordered = append(ordered, entry{h.Versions[0].Seq, h.Versions[len(h.Versions)-1].Span})
		}
	}
	sort.Slice(ordered, func(i, j int) bool {
		if ordered[i].first == ordered[j].first {
			return ordered[i].span.Id < ordered[j].span.Id
		}
		return ordered[i].first < ordered[j].first
	})
	for _, item := range ordered {
		span := item.span
		c.spans[span.Id] = span
		label := span.Label
		if label == nil || label.Attrs == nil {
			continue
		}
		switch label.Kind {
		case "workstream":
			attrs := &searchv1.WorkstreamAttrs{}
			if err := proto.Unmarshal(label.Attrs.Value, attrs); err != nil {
				return nil, err
			}
			if attrs.Probe {
				continue
			}
			idea := "span:" + span.Id
			if attrs.GetHypothesisId() != "" {
				idea = "hypothesis:" + attrs.GetHypothesisId()
			}
			c.owners[span.Id] = idea
			if _, ok := c.groups[idea]; !ok {
				c.order = append(c.order, idea)
			}
			c.groups[idea] = append(c.groups[idea], span)
		case "gate":
			attrs := &searchv1.GateAttrs{}
			if err := proto.Unmarshal(label.Attrs.Value, attrs); err != nil {
				return nil, err
			}
			c.inputs[span.Id] = attrs.Subject == searchv1.GateSubject_GATE_SUBJECT_INPUT
		case "comparison":
			attrs := &searchv1.ComparisonAttrs{}
			if err := proto.Unmarshal(label.Attrs.Value, attrs); err != nil {
				return nil, err
			}
			for _, link := range label.Links {
				if link.Rel == "candidate" {
					c.comparisons[link.To] = attrs.Verdict
				}
			}
		}
	}
	return c, nil
}
func (c *performanceContext) owner(at string) string {
	seen := map[string]bool{}
	for at != "" && !seen[at] {
		if _, ok := c.owners[at]; ok {
			return at
		}
		seen[at] = true
		at = c.spans[at].GetParent()
	}
	return ""
}
func passed(span *storev1.Span) bool {
	return span.GetLabel().GetVerdict() == "passed" || span.GetLabel().GetVerdict() == "accepted"
}
func (c *performanceContext) outcome(pointSpan, idea, owner string) storev1.IdeaOutcome {
	if c.inputs[pointSpan] {
		return storev1.IdeaOutcome_IDEA_OUTCOME_INPUT
	}
	attempts := c.groups[idea]
	if len(attempts) == 0 {
		return storev1.IdeaOutcome_IDEA_OUTCOME_UNATTRIBUTED
	}
	anyPassed := false
	for _, span := range attempts {
		switch span.Status {
		case storev1.SpanStatus_SPAN_STATUS_PENDING, storev1.SpanStatus_SPAN_STATUS_RUNNING, storev1.SpanStatus_SPAN_STATUS_WAITING:
			return storev1.IdeaOutcome_IDEA_OUTCOME_PENDING
		}
		anyPassed = anyPassed || passed(span)
	}
	if !anyPassed || (owner != "" && !passed(c.spans[owner])) {
		return storev1.IdeaOutcome_IDEA_OUTCOME_FAILED
	}
	switch c.comparisons[owner] {
	case searchv1.ComparisonVerdict_COMPARISON_VERDICT_WITHIN_NOISE:
		return storev1.IdeaOutcome_IDEA_OUTCOME_WITHIN_NOISE
	case searchv1.ComparisonVerdict_COMPARISON_VERDICT_WORSE:
		return storev1.IdeaOutcome_IDEA_OUTCOME_NOT_KEPT
	default:
		return storev1.IdeaOutcome_IDEA_OUTCOME_KEPT
	}
}
func measuredInput(state *storev1.Keyframe, epoch *storev1.EpochStarted, metric string, c *performanceContext) *float64 {
	fact := epoch.GetRun().GetInputMeasurement()
	if fact != nil {
		if m := fact.GetMeasured(); m != nil && m.Series == metric {
			return proto.Float64(m.Value)
		}
		if fact.GetMeasured() == nil {
			return nil
		}
	}
	for _, h := range state.Metrics {
		if h.Series != metric {
			continue
		}
		for _, v := range h.Points {
			if p := v.Point; p != nil && (p.GetSpan() == "" || c.inputs[p.GetSpan()]) {
				return proto.Float64(p.Value)
			}
		}
	}
	return nil
}
func decide(series *storev1.SeriesPerformance, r *storev1.IdeaPerformance, d storev1.Direction) {
	if r.Outcome != storev1.IdeaOutcome_IDEA_OUTCOME_KEPT || series.Refusal != nil {
		if r.Outcome == storev1.IdeaOutcome_IDEA_OUTCOME_KEPT {
			r.Outcome = storev1.IdeaOutcome_IDEA_OUTCOME_NOT_KEPT
		}
		return
	}
	value := r.GetPoint().GetValue()
	if series.InputValue != nil && ((d == storev1.Direction_DIRECTION_MIN && value > *series.InputValue) || (d != storev1.Direction_DIRECTION_MIN && value < *series.InputValue)) {
		r.Outcome = storev1.IdeaOutcome_IDEA_OUTCOME_WORSE_THAN_INPUT
		return
	}
	if series.BestValue != nil && !((d == storev1.Direction_DIRECTION_MIN && value < *series.BestValue) || (d != storev1.Direction_DIRECTION_MIN && value > *series.BestValue)) {
		r.Outcome = storev1.IdeaOutcome_IDEA_OUTCOME_NOT_KEPT
		return
	}
	r.NewBest = true
	series.BestValue = proto.Float64(value)
}
func (c *performanceContext) decisions(series *storev1.SeriesPerformance) []*storev1.IdeaDecision {
	var result []*storev1.IdeaDecision
	for _, idea := range c.order {
		attempts := c.groups[idea]
		var selected, last *storev1.IdeaPerformance
		newBest := false
		for _, r := range series.Points {
			if r.GetIdea() != idea {
				continue
			}
			last = r
			newBest = newBest || r.NewBest
			if r.Outcome != storev1.IdeaOutcome_IDEA_OUTCOME_FAILED && r.Outcome != storev1.IdeaOutcome_IDEA_OUTCOME_PENDING {
				selected = r
			}
		}
		if selected == nil {
			selected = last
		}
		owner := attempts[len(attempts)-1].Id
		for _, span := range attempts {
			if passed(span) {
				owner = span.Id
			}
		}
		outcome := c.outcome("", idea, owner)
		if outcome == storev1.IdeaOutcome_IDEA_OUTCOME_KEPT {
			outcome = storev1.IdeaOutcome_IDEA_OUTCOME_NOT_KEPT
			if selected != nil {
				outcome = selected.Outcome
			}
		}
		decision := &storev1.IdeaDecision{Idea: idea, Outcome: outcome, NewBest: newBest}
		if newBest {
			decision.Outcome = storev1.IdeaOutcome_IDEA_OUTCOME_KEPT
		}
		if selected != nil {
			decision.MeasurementSeq = proto.Uint64(selected.MeasurementSeq)
		}
		result = append(result, decision)
	}
	return result
}

// projectPerformance restores core facts; frontends never choose the best reading.
func projectPerformance(state *storev1.Keyframe, epoch *storev1.EpochStarted) ([]*storev1.SeriesPerformance, error) {
	c, err := performanceSpans(state)
	if err != nil {
		return nil, err
	}
	var result []*storev1.SeriesPerformance
	for _, objective := range epoch.GetRun().GetObjectives() {
		series := &storev1.SeriesPerformance{Series: objective.Series}
		if objective.Direction != storev1.Direction_DIRECTION_MIN && objective.Direction != storev1.Direction_DIRECTION_MAX {
			v := storev1.PerformanceRefusal_PERFORMANCE_REFUSAL_UNKNOWN_DIRECTION
			series.Refusal = &v
		}
		series.InputValue = measuredInput(state, epoch, objective.Series, c)
		if series.InputValue != nil {
			series.BestValue = proto.Float64(*series.InputValue)
		}
		var versions []*storev1.MetricVersion
		for _, h := range state.Metrics {
			if h.Series == objective.Series {
				versions = append(versions, h.Points...)
				break
			}
		}
		sort.SliceStable(versions, func(i, j int) bool {
			if versions[i].GetPoint().GetClock() == versions[j].GetPoint().GetClock() {
				return versions[i].Seq < versions[j].Seq
			}
			return versions[i].GetPoint().GetClock() < versions[j].GetPoint().GetClock()
		})
		selected := map[string]uint64{}
		for _, v := range versions {
			if v.Point == nil {
				continue
			}
			owner := c.owner(v.Point.GetSpan())
			idea := c.owners[owner]
			r := &storev1.IdeaPerformance{MeasurementSeq: v.Seq, Point: proto.Clone(v.Point).(*storev1.MetricPoint), Outcome: c.outcome(v.Point.GetSpan(), idea, owner)}
			if idea != "" {
				r.Idea = proto.String(idea)
			}
			series.Points = append(series.Points, r)
			switch r.Outcome {
			case storev1.IdeaOutcome_IDEA_OUTCOME_KEPT, storev1.IdeaOutcome_IDEA_OUTCOME_NOT_KEPT, storev1.IdeaOutcome_IDEA_OUTCOME_WITHIN_NOISE, storev1.IdeaOutcome_IDEA_OUTCOME_WORSE_THAN_INPUT:
				if idea != "" {
					selected[idea] = r.MeasurementSeq
				}
			}
		}
		for _, r := range series.Points {
			if r.GetIdea() != "" && selected[r.GetIdea()] != r.MeasurementSeq && r.Outcome == storev1.IdeaOutcome_IDEA_OUTCOME_KEPT {
				r.Outcome = storev1.IdeaOutcome_IDEA_OUTCOME_NOT_KEPT
			}
			decide(series, r, objective.Direction)
		}
		series.Ideas = c.decisions(series)
		result = append(result, series)
	}
	return result, nil
}
