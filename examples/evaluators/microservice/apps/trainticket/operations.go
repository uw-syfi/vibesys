package trainticket

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"vibesys/microservice-evaluator/api"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

type expectationKind int

const (
	expectEnvelope expectationKind = iota
	expectExactData
	expectEntityList
	expectEntityAbsent
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
	release      func()
	commit       func()
}

func (a *Application) BuildOperation(operation api.Operation, sample api.Sample, prepared any) (api.OperationPlan, error) {
	data, ok := prepared.(*dataset)
	if !ok || data == nil || len(data.records) == 0 {
		return api.OperationPlan{}, fmt.Errorf("Train Ticket fixture is not prepared")
	}
	service := serviceFromOperation(operation.Name)
	itemIndex := sample.Random % uint64(len(data.records))
	item := &data.records[itemIndex]
	alternate := &data.records[(itemIndex+1)%uint64(len(data.records))]
	switch {
	case operation.Name == "create_read_delete_config":
		return a.buildEphemeral(operation, sample, data)
	case strings.HasPrefix(operation.Name, "list_"):
		return a.buildList(operation, service, item), nil
	case strings.HasPrefix(operation.Name, "read_"):
		return a.buildRead(operation, service, item), nil
	case strings.HasPrefix(operation.Name, "update_read_"):
		return a.buildUpdateRead(operation, service, item, alternate, sample), nil
	default:
		return api.OperationPlan{}, fmt.Errorf("unsupported Train Ticket operation %q", operation.Name)
	}
}

func (a *Application) FinishOperation(plan api.OperationPlan) {
	state, ok := plan.State.(*operationState)
	if ok && state.release != nil {
		state.release()
		state.release = nil
	}
}

func (a *Application) buildList(operation api.Operation, service string, item *record) api.OperationPlan {
	paths := map[string]string{
		"config": "/api/v1/configservice/configs", "station": "/api/v1/stationservice/stations",
		"train": "/api/v1/trainservice/trains", "travel": "/api/v1/travelservice/trips",
		"route": "/api/v1/routeservice/routes", "price": "/api/v1/priceservice/prices",
	}
	item.mu.RLock()
	return api.OperationPlan{
		Invocations: []api.Invocation{a.invocation(service, operation.Name, http.MethodGet, paths[service], nil)},
		State: &operationState{release: item.mu.RUnlock, expectations: []stepExpectation{{
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
	item.mu.RLock()
	path, expected := readPathAndValue(service, item)
	return api.OperationPlan{
		Invocations: []api.Invocation{a.invocation(service, operation.Name, http.MethodGet, path, nil)},
		State: &operationState{release: item.mu.RUnlock, expectations: []stepExpectation{{
			status: http.StatusOK, appStatus: 1, kind: expectExactData, service: service, expected: expected,
		}}},
	}
}

func (a *Application) buildUpdateRead(operation api.Operation, service string, item, alternate *record, sample api.Sample) api.OperationPlan {
	item.mu.Lock()
	version := sample.Random ^ uint64(sample.Counter)*0x9e3779b97f4a7c15
	retiredStationName := item.stationA.Name
	retiredRouteStart := item.route.StartStationID
	retiredRouteTerminal := item.route.TerminalStationID
	retiredPriceRoute := item.price.RouteID
	retiredPriceTrain := item.price.TrainType
	method, path, body, readPath, expected, commit := updatedEntity(service, item, alternate, version)
	invocations := []api.Invocation{
		a.invocation(service, operation.Name, method, path, body),
		a.invocation(service, operation.Name, http.MethodGet, readPath, nil),
	}
	expectations := []stepExpectation{
		{status: expectedWriteStatus(service, method), appStatus: 1, kind: expectEnvelope, service: service},
		{status: http.StatusOK, appStatus: 1, kind: expectExactData, service: service, expected: expected},
	}
	switch service {
	case "station":
		invocations = append(invocations, a.invocation(
			service, operation.Name, http.MethodGet,
			"/api/v1/stationservice/stations/id/"+url.PathEscape(retiredStationName), nil,
		))
		expectations = append(expectations, stepExpectation{
			status: http.StatusOK, appStatus: 0, kind: expectEnvelope, service: service,
		})
	case "route":
		invocations = append(invocations, a.invocation(
			service, operation.Name, http.MethodGet,
			"/api/v1/routeservice/routes/"+retiredRouteStart+"/"+retiredRouteTerminal, nil,
		))
		expectations = append(expectations, stepExpectation{
			status: http.StatusOK, appStatus: 0, kind: expectEnvelope, service: service,
		})
	case "price":
		invocations = append(invocations, a.invocation(
			service, operation.Name, http.MethodGet,
			"/api/v1/priceservice/prices/"+retiredPriceRoute+"/"+retiredPriceTrain, nil,
		))
		expectations = append(expectations, stepExpectation{
			status: http.StatusOK, appStatus: 0, kind: expectEnvelope, service: service,
		})
	}
	return api.OperationPlan{
		Invocations: invocations,
		State: &operationState{
			release:      item.mu.Unlock,
			commit:       commit,
			expectations: expectations,
		},
	}
}

func (a *Application) buildEphemeral(operation api.Operation, sample api.Sample, data *dataset) (api.OperationPlan, error) {
	token := fmt.Sprintf("%s%016x%016x", data.namespace, uint64(sample.Counter), sample.Random)
	item := configEntity{Name: token, Value: strconv.FormatUint(sample.Random, 10), Description: fmt.Sprintf("d-%016x", sample.Random^0xa5a5a5a5a5a5a5a5)}
	path := "/api/v1/configservice/configs/" + url.PathEscape(item.Name)
	return api.OperationPlan{
		Invocations: []api.Invocation{
			a.invocation("config", operation.Name, http.MethodPost, "/api/v1/configservice/configs", item),
			a.invocation("config", operation.Name, http.MethodGet, path, nil),
			a.invocation("config", operation.Name, http.MethodDelete, path, nil),
			a.invocation("config", operation.Name, http.MethodGet, path, nil),
			a.invocation("config", operation.Name, http.MethodGet, "/api/v1/configservice/configs", nil),
		},
		State: &operationState{expectations: []stepExpectation{
			{status: http.StatusCreated, appStatus: 1, kind: expectEnvelope, service: "config"},
			{status: http.StatusOK, appStatus: 1, kind: expectExactData, service: "config", expected: item},
			{status: http.StatusOK, appStatus: 1, kind: expectEnvelope, service: "config"},
			{status: http.StatusOK, appStatus: 0, kind: expectEnvelope, service: "config"},
			{status: http.StatusOK, appStatus: 1, kind: expectEntityAbsent, service: "config", expected: item},
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

func updatedEntity(service string, item, alternate *record, version uint64) (method, path string, body any, readPath string, expected any, commit func()) {
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
		updated.Name = trainticketsupport.UpdatedStationName(version, false)
		updated.StayTime = trainticketsupport.UpdatedStationStayTime(version)
		return http.MethodPut, "/api/v1/stationservice/stations", updated,
			"/api/v1/stationservice/stations/name/" + updated.ID, updated.Name,
			func() { item.stationA = updated }
	case "train":
		updated := item.train
		updated.AverageSpeed = trainticketsupport.UpdatedTrainSpeed(version)
		updated.EconomyClass = trainticketsupport.UpdatedTrainEconomy(updated.EconomyClass)
		return http.MethodPut, "/api/v1/trainservice/trains", updated,
			"/api/v1/trainservice/trains/" + updated.ID, updated,
			func() { item.train = updated }
	case "route":
		updatedInput := item.routeIn
		updatedRoute := item.route
		if item.route.StartStationID == item.stationA.ID {
			updatedRoute.Stations = []string{item.stationB.ID, item.stationA.ID}
			updatedRoute.StartStationID = item.stationB.ID
			updatedRoute.TerminalStationID = item.stationA.ID
			updatedInput.StartStation = item.stationB.ID
			updatedInput.EndStation = item.stationA.ID
		} else {
			updatedRoute.Stations = []string{item.stationA.ID, item.stationB.ID}
			updatedRoute.StartStationID = item.stationA.ID
			updatedRoute.TerminalStationID = item.stationB.ID
			updatedInput.StartStation = item.stationA.ID
			updatedInput.EndStation = item.stationB.ID
		}
		updatedInput.StationList = strings.Join(updatedRoute.Stations, ",")
		updatedRoute.Distances = append([]int(nil), item.route.Distances...)
		updatedRoute.Distances[1] = trainticketsupport.UpdatedRouteDistance(updatedRoute.Distances[1], version)
		updatedInput.DistanceList = fmt.Sprintf("0,%d", updatedRoute.Distances[1])
		return http.MethodPost, "/api/v1/routeservice/routes", updatedInput,
			"/api/v1/routeservice/routes/" + updatedRoute.ID, updatedRoute,
			func() { item.routeIn, item.route = updatedInput, updatedRoute }
	case "price":
		updated := item.price
		if updated.RouteID == alternate.route.ID && updated.TrainType == item.train.ID {
			updated.RouteID = item.route.ID
			updated.TrainType = item.train.ID
		} else {
			updated.RouteID = alternate.route.ID
			updated.TrainType = item.train.ID
		}
		updated.BasicPriceRate, updated.FirstClassPriceRate = trainticketsupport.UpdatedPriceRates(version)
		return http.MethodPut, "/api/v1/priceservice/prices", updated,
			"/api/v1/priceservice/prices/" + updated.RouteID + "/" + updated.TrainType, updated,
			func() { item.price = updated }
	default:
		updatedInput := item.tripIn
		updated := item.trip
		updatedInput.EndTime = trainticketsupport.UpdatedTripEnd(updatedInput.EndTime, version)
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
