package trainticket

import (
	_ "embed"
	"fmt"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/accuracy/jsoncheck"
)

//go:embed seed_catalog.json
var seedCatalogJSON []byte

var contracts = map[string]jsoncheck.Contract{
	"config": {
		Name: "config",
		Fields: map[string]jsoncheck.FieldValidator{
			"name": jsoncheck.NonEmptyString, "value": jsoncheck.String,
			"description": jsoncheck.String,
		},
		Key: stringKey("name"),
	},
	"station": {
		Name: "station",
		Fields: map[string]jsoncheck.FieldValidator{
			"id": jsoncheck.NonEmptyString, "name": jsoncheck.NonEmptyString,
			"stayTime": jsoncheck.Integer,
		},
		Key: stringKey("id"),
	},
	"train": {
		Name: "train",
		Fields: map[string]jsoncheck.FieldValidator{
			"id": jsoncheck.NonEmptyString, "economyClass": jsoncheck.Integer,
			"confortClass": jsoncheck.Integer, "averageSpeed": jsoncheck.Integer,
		},
		Key: stringKey("id"),
	},
	"route": {
		Name: "route",
		Fields: map[string]jsoncheck.FieldValidator{
			"id": jsoncheck.NonEmptyString, "stations": jsoncheck.StringList,
			"distances": jsoncheck.IntegerList, "startStationId": jsoncheck.NonEmptyString,
			"terminalStationId": jsoncheck.NonEmptyString,
		},
		Key: stringKey("id"),
		ValidateObject: func(object jsoncheck.Object) error {
			stations := object["stations"].([]any)
			distances := object["distances"].([]any)
			if len(stations) != len(distances) {
				return fmt.Errorf("stations and distances lengths differ")
			}
			return nil
		},
	},
	"price": {
		Name: "price",
		Fields: map[string]jsoncheck.FieldValidator{
			"id": jsoncheck.NonEmptyString, "trainType": jsoncheck.NonEmptyString,
			"routeId": jsoncheck.NonEmptyString, "basicPriceRate": jsoncheck.Number,
			"firstClassPriceRate": jsoncheck.Number,
		},
		Key: stringKey("id"),
	},
	"travel": {
		Name: "travel",
		Fields: map[string]jsoncheck.FieldValidator{
			"tripId": jsoncheck.ExactObject(map[string]jsoncheck.FieldValidator{
				"type": tripType, "number": jsoncheck.NonEmptyString,
			}),
			"trainTypeId": jsoncheck.NonEmptyString, "routeId": jsoncheck.NonEmptyString,
			"startingTime": jsoncheck.Integer, "startingStationId": jsoncheck.NonEmptyString,
			"stationsId": jsoncheck.NonEmptyString, "terminalStationId": jsoncheck.NonEmptyString,
			"endTime": jsoncheck.Integer,
		},
		Key: tripKey,
	},
}

func loadSeedCatalog() (map[string][]jsoncheck.Object, error) {
	decoded, err := httpcheck.DecodeJSON(seedCatalogJSON)
	if err != nil {
		return nil, fmt.Errorf("decode embedded Train Ticket seed catalog: %w", err)
	}
	root, ok := decoded.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("embedded Train Ticket seed catalog must be an object")
	}
	if err := httpcheck.ExactFields(
		root,
		"config", "station", "train", "travel", "route", "price",
	); err != nil {
		return nil, fmt.Errorf("embedded Train Ticket seed catalog: %w", err)
	}
	catalog := make(map[string][]jsoncheck.Object, len(contracts))
	for service, contract := range contracts {
		items, ok := root[service].([]any)
		if !ok {
			return nil, fmt.Errorf("embedded %s seed catalog must be a list", service)
		}
		if _, err := contract.IndexList(items, "embedded "+service+" seed catalog"); err != nil {
			return nil, err
		}
		catalog[service] = make([]jsoncheck.Object, 0, len(items))
		for _, item := range items {
			catalog[service] = append(catalog[service], item.(map[string]any))
		}
	}
	return catalog, nil
}

func assertEntity(service string, actual any, expected any, where string) error {
	validated, err := contracts[service].Validate(actual, where)
	if err != nil {
		return err
	}
	equal, err := jsoncheck.Equal(validated, expected)
	if err != nil {
		return fmt.Errorf("%s: %w", where, err)
	}
	if !equal {
		normalized, _ := jsoncheck.Normalize(expected)
		return fmt.Errorf("%s: entity mismatch: actual=%v expected=%v", where, validated, normalized)
	}
	return nil
}

func normalizedObject(value any) (jsoncheck.Object, error) {
	normalized, err := jsoncheck.Normalize(value)
	if err != nil {
		return nil, err
	}
	object, ok := normalized.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("expected normalized object, got %T", normalized)
	}
	return object, nil
}

func stringKey(field string) jsoncheck.KeyFunc {
	return func(object jsoncheck.Object) (string, error) {
		value, ok := object[field].(string)
		if !ok || value == "" {
			return "", fmt.Errorf("%s must be a non-empty string", field)
		}
		return value, nil
	}
}

func tripKey(object jsoncheck.Object) (string, error) {
	tripID, ok := object["tripId"].(map[string]any)
	if !ok {
		return "", fmt.Errorf("tripId must be an object")
	}
	kind, kindOK := tripID["type"].(string)
	number, numberOK := tripID["number"].(string)
	if !kindOK || (kind != "G" && kind != "D") || !numberOK || number == "" {
		return "", fmt.Errorf("tripId must contain type G or D and a non-empty number")
	}
	return kind + number, nil
}

func tripType(value any) error {
	text, ok := value.(string)
	if !ok || (text != "G" && text != "D") {
		return fmt.Errorf("expected G or D, got %v", value)
	}
	return nil
}
