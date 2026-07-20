package trainticket

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

type expectationKind int

const (
	expectEnvelope expectationKind = iota
	expectExactData
	expectEntityList
)

type stepExpectation struct {
	status    int
	appStatus int
	kind      expectationKind
	service   string
	expected  any
}

type operationState struct {
	expectations []stepExpectation
	locked       *record
	commit       func()
}

func (a *Application) BuildOperation(operation api.Operation, sample api.Sample, prepared any) (api.OperationPlan, error) {
	data, ok := prepared.(*dataset)
	if !ok || data == nil || len(data.records) == 0 {
		return api.OperationPlan{}, fmt.Errorf("Train Ticket fixture is not prepared")
	}
	service := serviceFromOperation(operation.Name)
	item := &data.records[sample.Random%uint64(len(data.records))]
	switch {
	case operation.Name == "create_read_delete_config":
		return a.buildEphemeral(operation, sample, data)
	case strings.HasPrefix(operation.Name, "list_"):
		return a.buildList(operation, service, item), nil
	case strings.HasPrefix(operation.Name, "read_"):
		return a.buildRead(operation, service, item), nil
	case strings.HasPrefix(operation.Name, "update_read_"):
		return a.buildUpdateRead(operation, service, item, sample), nil
	default:
		return api.OperationPlan{}, fmt.Errorf("unsupported Train Ticket operation %q", operation.Name)
	}
}

func (a *Application) FinishOperation(plan api.OperationPlan) {
	state, ok := plan.State.(*operationState)
	if ok && state.locked != nil {
		state.locked.mu.Unlock()
		state.locked = nil
	}
}

func (a *Application) buildList(operation api.Operation, service string, item *record) api.OperationPlan {
	paths := map[string]string{
		"config": "/api/v1/configservice/configs", "station": "/api/v1/stationservice/stations",
		"train": "/api/v1/trainservice/trains", "travel": "/api/v1/travelservice/trips",
		"route": "/api/v1/routeservice/routes", "price": "/api/v1/priceservice/prices",
	}
	item.mu.Lock()
	return api.OperationPlan{
		Invocations: []api.Invocation{a.invocation(service, operation.Name, http.MethodGet, paths[service], nil)},
		State: &operationState{locked: item, expectations: []stepExpectation{{
			status: http.StatusOK, appStatus: 1, kind: expectEntityList,
			service: service, expected: listExpectedValue(service, item),
		}}},
	}
}

func listExpectedValue(service string, item *record) any {
	switch service {
	case "config":
		return item.config
	case "station":
		return item.stationA
	case "train":
		return item.train
	case "route":
		return item.route
	case "price":
		return item.price
	default:
		return item.trip
	}
}

func (a *Application) buildRead(operation api.Operation, service string, item *record) api.OperationPlan {
	item.mu.Lock()
	path, expected := readPathAndValue(service, item)
	return api.OperationPlan{
		Invocations: []api.Invocation{a.invocation(service, operation.Name, http.MethodGet, path, nil)},
		State: &operationState{locked: item, expectations: []stepExpectation{{
			status: http.StatusOK, appStatus: 1, kind: expectExactData, service: service, expected: expected,
		}}},
	}
}

func (a *Application) buildUpdateRead(operation api.Operation, service string, item *record, sample api.Sample) api.OperationPlan {
	item.mu.Lock()
	version := sample.Random ^ uint64(sample.Counter)*0x9e3779b97f4a7c15
	method, path, body, readPath, expected, commit := updatedEntity(service, item, version)
	return api.OperationPlan{
		Invocations: []api.Invocation{
			a.invocation(service, operation.Name, method, path, body),
			a.invocation(service, operation.Name, http.MethodGet, readPath, nil),
		},
		State: &operationState{
			locked: item,
			commit: commit,
			expectations: []stepExpectation{
				{status: expectedWriteStatus(service, method), appStatus: 1, kind: expectEnvelope, service: service},
				{status: http.StatusOK, appStatus: 1, kind: expectExactData, service: service, expected: expected},
			},
		},
	}
}

func (a *Application) buildEphemeral(operation api.Operation, sample api.Sample, data *dataset) (api.OperationPlan, error) {
	token := fmt.Sprintf("%s-ephemeral-%d-%016x", data.namespace, sample.Counter, sample.Random)
	item := configEntity{Name: token, Value: strconv.FormatUint(sample.Random, 10), Description: token + "-description"}
	path := "/api/v1/configservice/configs/" + url.PathEscape(item.Name)
	return api.OperationPlan{
		Invocations: []api.Invocation{
			a.invocation("config", operation.Name, http.MethodPost, "/api/v1/configservice/configs", item),
			a.invocation("config", operation.Name, http.MethodGet, path, nil),
			a.invocation("config", operation.Name, http.MethodDelete, path, nil),
			a.invocation("config", operation.Name, http.MethodGet, path, nil),
		},
		State: &operationState{expectations: []stepExpectation{
			{status: http.StatusCreated, appStatus: 1, kind: expectEnvelope, service: "config"},
			{status: http.StatusOK, appStatus: 1, kind: expectExactData, service: "config", expected: item},
			{status: http.StatusOK, appStatus: 1, kind: expectEnvelope, service: "config"},
			{status: http.StatusOK, appStatus: 0, kind: expectEnvelope, service: "config"},
		}},
	}, nil
}

func readPathAndValue(service string, item *record) (string, any) {
	switch service {
	case "config":
		return "/api/v1/configservice/configs/" + url.PathEscape(item.config.Name), item.config
	case "station":
		return "/api/v1/stationservice/stations/name/" + item.stationA.ID, item.stationA.Name
	case "train":
		return "/api/v1/trainservice/trains/" + item.train.ID, item.train
	case "route":
		return "/api/v1/routeservice/routes/" + item.route.ID, item.route
	case "price":
		return "/api/v1/priceservice/prices/" + item.price.RouteID + "/" + item.price.TrainType, item.price
	default:
		return "/api/v1/travelservice/trips/" + item.tripIn.TripID, item.trip
	}
}

func updatedEntity(service string, item *record, version uint64) (method, path string, body any, readPath string, expected any, commit func()) {
	switch service {
	case "config":
		updated := item.config
		updated.Value = fmt.Sprintf("v-%016x", version)
		updated.Description = fmt.Sprintf("d-%016x", version^0xa5a5a5a5a5a5a5a5)
		return http.MethodPut, "/api/v1/configservice/configs", updated,
			"/api/v1/configservice/configs/" + url.PathEscape(updated.Name), updated,
			func() { item.config = updated }
	case "station":
		updated := item.stationA
		updated.Name = fmt.Sprintf("Station-%016x", version)
		updated.StayTime = 1 + int(version%120)
		return http.MethodPut, "/api/v1/stationservice/stations", updated,
			"/api/v1/stationservice/stations/name/" + updated.ID, updated.Name,
			func() { item.stationA = updated }
	case "train":
		updated := item.train
		updated.AverageSpeed = 80 + int(version%420)
		updated.EconomyClass = 100 + int((version>>8)%900)
		return http.MethodPut, "/api/v1/trainservice/trains", updated,
			"/api/v1/trainservice/trains/" + updated.ID, updated,
			func() { item.train = updated }
	case "route":
		updatedInput := item.routeIn
		updatedRoute := item.route
		updatedRoute.Stations = append([]string(nil), item.route.Stations...)
		updatedRoute.Distances = append([]int(nil), item.route.Distances...)
		updatedRoute.Distances[1] = 100 + int(version%1800)
		updatedInput.DistanceList = fmt.Sprintf("0,%d", updatedRoute.Distances[1])
		return http.MethodPost, "/api/v1/routeservice/routes", updatedInput,
			"/api/v1/routeservice/routes/" + updatedRoute.ID, updatedRoute,
			func() { item.routeIn, item.route = updatedInput, updatedRoute }
	case "price":
		updated := item.price
		updated.BasicPriceRate = 0.1 + float64(version%8000)/10000
		updated.FirstClassPriceRate = 0.9 + float64((version>>8)%10000)/10000
		return http.MethodPut, "/api/v1/priceservice/prices", updated,
			"/api/v1/priceservice/prices/" + updated.RouteID + "/" + updated.TrainType, updated,
			func() { item.price = updated }
	default:
		updatedInput := item.tripIn
		updated := item.trip
		updatedInput.EndTime = updatedInput.StartingTime + int64(time.Minute/time.Millisecond)*int64(60+version%600)
		updated.EndTime = updatedInput.EndTime
		return http.MethodPut, "/api/v1/travelservice/trips", updatedInput,
			"/api/v1/travelservice/trips/" + updatedInput.TripID, updated,
			func() { item.tripIn, item.trip = updatedInput, updated }
	}
}

func expectedWriteStatus(service, method string) int {
	if method == http.MethodPost && (service == "config" || service == "station" || service == "price" || service == "travel") {
		return http.StatusCreated
	}
	return http.StatusOK
}
