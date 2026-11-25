package core

// HeadRelation is how a commit stands to its task's result branch head in the history.
type HeadRelation string

// The relations of a commit to the result branch head.
const (
	// ContainsHead is the head itself or a descendant of it: promote can fast-forward to it.
	ContainsHead HeadRelation = "contains-head"
	// InHead is an ancestor of the head, a commit the branch already holds, such as the base. A
	// measure of it is a reference for comparisons, not a candidate.
	InHead HeadRelation = "in-head"
	// DivergedFromHead neither contains the head nor is held by it: a candidate built on an older
	// head. Promote refuses it until it is rebased onto the head and measured again.
	DivergedFromHead HeadRelation = "diverged"
)

// RelationToHead is the relation of a commit to the head, from whether the commit contains the head
// and whether the head contains the commit. A commit that does both is the head.
func RelationToHead(containsHead, inHead bool) HeadRelation {
	switch {
	case containsHead:
		return ContainsHead
	case inHead:
		return InHead
	default:
		return DivergedFromHead
	}
}

// Supersedable says whether a later move of the result branch can make the job's work useless: a
// measure that has not ended, whose hours on the placement are still to be spent or being spent.
// A job that ended has spent them, and a check or command job is cheap and says nothing promote
// reads by itself.
func Supersedable(j Job) bool {
	return j.Spec.Settings != nil && j.Spec.Settings.Kind() == JobMeasure && !j.State.Final()
}

// Superseded says whether the job is a measure still to finish whose commit, standing to the
// result branch head as relation says, can no longer be promoted: its numbers cannot lead to a
// promotion. The lead cancels it, or rebases the candidate onto the head and measures that.
func Superseded(j Job, relation HeadRelation) bool {
	return Supersedable(j) && relation == DivergedFromHead
}
