package core

import (
	"slices"
	"time"
)

// The frontier of a multi-objective task is the set of its commits that no other member beats:
// each was measured, rests on a passing check, and when it was admitted it was a tradeoff against
// every member it did not evict. It is where an optimization keeps the commits that are better on
// some objective and worse on another, which the result branch, with its one head, cannot hold.
//
// Membership is decided here, from saved results alone, with the comparison compare uses: a
// candidate is judged against each member on every objective (CompareCommits).

// FrontierMember is one commit on a task's frontier.
type FrontierMember struct {
	Commit Commit
	// AddedAt is when it was admitted.
	AddedAt time.Time
}

// Frontier is a task's frontier as a store holds it.
type Frontier struct {
	// Revision counts the changes the frontier has had: zero for one that never changed. A change
	// is decided against one revision, and a store refuses it against another.
	Revision uint64
	// Members are in the order they were admitted, and no commit is in it twice.
	Members []FrontierMember
}

// Has reports whether a commit is a member.
func (f Frontier) Has(c Commit) bool {
	return slices.ContainsFunc(f.Members, func(m FrontierMember) bool { return m.Commit == c })
}

// FrontierRefusalKind is one reason a commit was not admitted to the frontier.
type FrontierRefusalKind string

// The refusal kinds, in the order a refusal lists them.
const (
	// FrontierUnmeasured is a candidate, or a member it is judged against, with no measurement.
	FrontierUnmeasured FrontierRefusalKind = "unmeasured"
	// FrontierCheckMissing is a measurement of the candidate that does not rest on a passing check
	// of its commit, task version and environment.
	FrontierCheckMissing FrontierRefusalKind = "check_missing"
	// FrontierDominated is a member that beats the candidate: the candidate is worse than it on
	// some objective and better on none.
	FrontierDominated FrontierRefusalKind = "dominated"
	// FrontierWithinNoise is a member the candidate is within noise of on every objective, so it
	// would add nothing the member does not stand for.
	FrontierWithinNoise FrontierRefusalKind = "within_noise"
	// FrontierInconclusive is a member the samples cannot place the candidate against.
	FrontierInconclusive FrontierRefusalKind = "inconclusive"
	// FrontierStale is a member measured under another task version than the candidate: the two
	// are not comparable, and the candidate is not admitted beside a member it cannot be judged
	// against.
	FrontierStale FrontierRefusalKind = "stale"
	// FrontierEnvironmentMismatch is a member measured in another environment than the candidate.
	FrontierEnvironmentMismatch FrontierRefusalKind = "environment_mismatch"
)

var frontierRefusalOrder = []FrontierRefusalKind{
	FrontierUnmeasured, FrontierCheckMissing, FrontierDominated, FrontierWithinNoise,
	FrontierInconclusive, FrontierStale, FrontierEnvironmentMismatch,
}

// FrontierRefusalReason is one reason with the evidence it rests on.
type FrontierRefusalReason struct {
	Kind    FrontierRefusalKind
	Message string
	// Member is the member the reason is about. It is empty for a reason about the candidate
	// alone: its own missing measurement or check.
	Member Commit
	// Verdict is the overall verdict of the candidate against Member, and Objectives the verdict on
	// each objective, for dominated, within_noise, inconclusive and stale.
	Verdict    *Verdict
	Objectives []ObjectiveVerdict
}

// FrontierRefusal is a normal answer to a request to add a commit, not an error. It carries every
// reason that applies.
type FrontierRefusal struct {
	Reasons []FrontierRefusalReason
}

// FrontierAdmission is the frontier a decision admitted a commit to, and the only change a Store
// makes to a frontier. Its fields are private, so outside this package only DecideFrontier makes
// one that holds (AssumeAdmitted is for store tests, and archcheck keeps it there), and the zero
// value admits nothing: a store refuses it.
type FrontierAdmission struct {
	from    uint64
	added   FrontierMember
	evicted []Commit
	members []FrontierMember
	already bool
	valid   bool
}

// From is the revision of the frontier the decision was made against.
func (a FrontierAdmission) From() uint64 { return a.from }

// Added is the member admitted.
func (a FrontierAdmission) Added() FrontierMember { return a.added }

// Evicted are the members the admitted commit beats, which leave the frontier, in the order they
// were admitted.
func (a FrontierAdmission) Evicted() []Commit { return slices.Clone(a.evicted) }

// Members is the frontier after the change.
func (a FrontierAdmission) Members() []FrontierMember { return slices.Clone(a.members) }

// AlreadyMember reports that the commit was a member before, so nothing changes.
func (a FrontierAdmission) AlreadyMember() bool { return a.already }

// Valid reports whether a decision admitted it: false for the zero value.
func (a FrontierAdmission) Valid() bool { return a.valid }

// AssumeAdmitted makes a change of the frontier from a revision to the members without a decision.
// It is for tests of stores, which hold frontiers that no decision would make. Only test files and
// internal/store/storetest may use it (archcheck).
func AssumeAdmitted(from uint64, members []FrontierMember) FrontierAdmission {
	a := FrontierAdmission{from: from, members: slices.Clone(members), valid: true}
	if len(members) > 0 {
		a.added = members[len(members)-1]
	}
	return a
}

// FrontierDecision is the answer to a request to add a commit: a FrontierAdmission or a
// FrontierRefusal.
type FrontierDecision interface {
	frontierDecision()
}

func (FrontierAdmission) frontierDecision() {}
func (FrontierRefusal) frontierDecision()   {}

// FrontierRequest asks whether a commit joins a frontier.
type FrontierRequest struct {
	Candidate Commit
	Frontier  Frontier
	// Results are saved results of the candidate and of the members. Those of other commits are
	// ignored.
	Results []Result
	// At is when the commit would be admitted.
	At time.Time
}

// DecideFrontier decides whether the candidate joins the frontier. A commit that is a member is
// admitted again with nothing changed, so asking twice is asking once. Otherwise it is judged
// against each member with the comparison of CompareCommits, on every objective:
//
//   - a member it is better than (better on some objective, worse on none) is evicted;
//   - a member it is a tradeoff against stays;
//   - a member it is worse than refuses it as FrontierDominated, one it is within noise of as
//     FrontierWithinNoise, one it cannot be placed against as FrontierInconclusive, and one
//     measured under another task version as FrontierStale; FrontierEnvironmentMismatch is a
//     member measured in another environment, and FrontierUnmeasured one with no measurement.
//
// A candidate with no measurement is refused as FrontierUnmeasured and is judged no further; one
// with a measurement that rests on no passing check is refused as FrontierCheckMissing. Every
// reason that applies is returned, in the order of the kinds and, within a kind, of the members.
// The admission keeps the members that stay in their order and adds the candidate last.
//
// It fails with a *Failure, rather than refusing, for a request with no time, for a candidate whose
// latest measurement is not of a multi-objective task (FailureInvalidInput: a single-metric task
// has a result branch and no frontier), and when two sides cannot be compared at all (see
// Compare).
func DecideFrontier(req FrontierRequest) (FrontierDecision, error) {
	if req.At.IsZero() {
		return nil, failf(FailureInvalidInput, "frontier: no time")
	}
	if req.Frontier.Has(req.Candidate) {
		members := slices.Clone(req.Frontier.Members)
		added := members[slices.IndexFunc(members, func(m FrontierMember) bool { return m.Commit == req.Candidate })]
		return FrontierAdmission{from: req.Frontier.Revision, added: added, members: members, already: true, valid: true}, nil
	}
	candidate := measurementsOf(req.Candidate, req.Results)
	if len(candidate) == 0 {
		return FrontierRefusal{Reasons: []FrontierRefusalReason{{
			Kind: FrontierUnmeasured, Message: "commit " + shortCommit(req.Candidate) + " has no measurement",
		}}}, nil
	}
	if !objectivesOf(candidate[len(candidate)-1]).Pareto() {
		return nil, failf(FailureInvalidInput,
			"commit %s was measured on one metric: a frontier holds the commits of a task whose objectives.toml has a [pareto] section and more than one objective",
			shortCommit(req.Candidate))
	}
	var reasons []FrontierRefusalReason
	if slices.ContainsFunc(candidate, func(r Result) bool { return !restsOnPassingCheck(r, req.Results) }) {
		reasons = append(reasons, FrontierRefusalReason{
			Kind:    FrontierCheckMissing,
			Message: "a measurement of commit " + shortCommit(req.Candidate) + " does not rest on a passing check of the same commit, task version and environment",
		})
	}
	var kept []FrontierMember
	var evicted []Commit
	for _, member := range req.Frontier.Members {
		reason, verdict, err := judgeAgainst(req.Candidate, candidate, member.Commit, req.Results)
		if err != nil {
			return nil, err
		}
		switch {
		case reason != nil:
			reasons = append(reasons, *reason)
		case verdict == Better:
			evicted = append(evicted, member.Commit)
		default:
			kept = append(kept, member)
		}
	}
	if len(reasons) > 0 {
		slices.SortStableFunc(reasons, func(a, b FrontierRefusalReason) int {
			return slices.Index(frontierRefusalOrder, a.Kind) - slices.Index(frontierRefusalOrder, b.Kind)
		})
		return FrontierRefusal{Reasons: reasons}, nil
	}
	added := FrontierMember{Commit: req.Candidate, AddedAt: req.At}
	return FrontierAdmission{
		from: req.Frontier.Revision, added: added, evicted: evicted,
		members: append(kept, added), valid: true,
	}, nil
}

// judgeAgainst compares the candidate with one member, and returns the reason the member refuses
// it, or the overall verdict when it does not: better or tradeoff.
func judgeAgainst(commit Commit, candidate []Result, member Commit, results []Result) (*FrontierRefusalReason, VerdictKind, error) {
	base := measurementsOf(member, results)
	if len(base) == 0 {
		return &FrontierRefusalReason{
			Kind: FrontierUnmeasured, Member: member,
			Message: "the member " + shortCommit(member) + " has no measurement to compare with",
		}, "", nil
	}
	compared, err := compareMeasurements(candidate, base, 0)
	if err != nil {
		return nil, "", err
	}
	if compared.mixedEnvironments != "" {
		return &FrontierRefusalReason{Kind: FrontierEnvironmentMismatch, Member: member, Message: compared.mixedEnvironments}, "", nil
	}
	v := compared.verdict
	refuse := func(kind FrontierRefusalKind, text string) (*FrontierRefusalReason, VerdictKind, error) {
		if len(compared.objectives) > 0 {
			text += " (" + describeObjectives(compared.objectives) + ")"
		}
		return &FrontierRefusalReason{Kind: kind, Member: member, Message: text, Verdict: &v, Objectives: compared.objectives}, "", nil
	}
	short, of := shortCommit(commit), shortCommit(member)
	switch {
	case v.Kind == Worse:
		return refuse(FrontierDominated, "the member "+of+" beats commit "+short+": it is better on some objective and worse on none")
	case v.Kind == WithinNoise:
		return refuse(FrontierWithinNoise, "commit "+short+" is within noise of the member "+of+" on every objective")
	case v.Kind == Inconclusive && v.Reason == ReasonTaskVersionMismatch:
		return refuse(FrontierStale, "commit "+short+" and the member "+of+" were measured under different task versions")
	case v.Kind == Inconclusive:
		return refuse(FrontierInconclusive, "the samples cannot place commit "+short+" against the member "+of+" ("+string(v.Reason)+")")
	}
	return nil, v.Kind, nil
}

// FrontierEntry is a member with its headline number on each objective.
type FrontierEntry struct {
	Member FrontierMember
	Values []ObjectiveValue
}

// FrontierEntries returns each member of a frontier, in its order, with the headline numbers
// ObjectiveValues gives it over the results.
func FrontierEntries(f Frontier, results []Result) []FrontierEntry {
	out := make([]FrontierEntry, 0, len(f.Members))
	for _, m := range f.Members {
		out = append(out, FrontierEntry{Member: m, Values: ObjectiveValues(m.Commit, results)})
	}
	return out
}
