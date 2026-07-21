package socialnetwork

import (
	"fmt"
	"net/http"
	"strconv"

	"vibesys/microservice-evaluator/api"
)

func (a *Application) BuildOperation(operation api.Operation, sample api.Sample, prepared any) (api.OperationPlan, error) {
	data, ok := prepared.(*dataset)
	if prepared == nil {
		data = a.makeDataset(api.TrialContext{Seed: a.seed})
		ok = true
	}
	if !ok || data == nil || len(data.users) < 2 {
		return api.OperationPlan{}, fmt.Errorf("Social Network fixture is not prepared")
	}
	user := &data.users[sample.Random%uint64(len(data.users))]
	switch operation.Name {
	case userTimelineRead:
		user.mu.RLock()
		return api.OperationPlan{
			Invocations: []api.Invocation{a.timelineInvocation(operation, userTimelineRead, user)},
			State: &operationState{
				kind: userTimelineRead, user: user, timelineSize: a.config.TimelineLimit, release: user.mu.RUnlock,
			},
		}, nil
	case homeTimelineRead:
		followee := &data.users[user.followee]
		followee.mu.RLock()
		return api.OperationPlan{
			Invocations: []api.Invocation{a.timelineInvocation(operation, homeTimelineRead, user)},
			State: &operationState{
				kind: homeTimelineRead, user: followee, timelineSize: a.config.TimelineLimit, release: followee.mu.RUnlock,
			},
		}, nil
	case composeUserTimeline:
		user.mu.Lock()
		marker := fmt.Sprintf("live_%s_%016x_%016x", data.namespace, uint64(sample.Counter), sample.Random)
		return api.OperationPlan{
			Invocations: []api.Invocation{
				{Target: operation.Target, Operation: operation.Name, Payload: a.composeRequest(user, marker)},
				a.timelineInvocation(operation, userTimelineRead, user),
			},
			State: &operationState{
				kind: composeUserTimeline, user: user, marker: marker,
				timelineSize: a.config.TimelineLimit, release: user.mu.Unlock,
			},
		}, nil
	case legacyComposePost:
		user.mu.Lock()
		marker := fmt.Sprintf("live_%s_%016x_%016x", data.namespace, uint64(sample.Counter), sample.Random)
		return api.OperationPlan{
			Invocations: []api.Invocation{{
				Target: operation.Target, Operation: operation.Name, Payload: a.composeRequest(user, marker),
			}},
			State: &operationState{kind: legacyComposePost, user: user, marker: marker, release: user.mu.Unlock},
		}, nil
	default:
		return api.OperationPlan{}, fmt.Errorf("unknown Social Network operation %q", operation.Name)
	}
}

func (a *Application) FinishOperation(plan api.OperationPlan) {
	state, ok := plan.State.(*operationState)
	if ok && state.release != nil {
		state.release()
		state.release = nil
	}
}

func (a *Application) timelineInvocation(operation api.Operation, kind string, user *benchmarkUser) api.Invocation {
	path := "/wrk2-api/user-timeline/read"
	if kind == homeTimelineRead {
		path = "/wrk2-api/home-timeline/read"
	}
	return api.Invocation{
		Target: operation.Target, Operation: operation.Name,
		Payload: api.HTTPRequestSpec{
			Method: http.MethodGet,
			Path:   path,
			Query: map[string]string{
				"user_id": strconv.Itoa(user.id), "start": "0", "stop": strconv.Itoa(a.config.TimelineLimit),
			},
		},
	}
}
