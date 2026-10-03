package hotel

import (
	"fmt"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/accuracy/jsoncheck"
)

type feature struct {
	id          string
	name, phone string
	lat, lon    float64
	raw         map[string]any
}

func decodeFeatureCollection(value any) (map[string]feature, error) {
	root, ok := value.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("GeoJSON root must be an object, got %T", value)
	}
	if err := httpcheck.ExactFields(root, "type", "features"); err != nil {
		return nil, fmt.Errorf("GeoJSON root: %w", err)
	}
	if root["type"] != "FeatureCollection" {
		return nil, fmt.Errorf("GeoJSON root type = %v, expected FeatureCollection", root["type"])
	}
	items, ok := root["features"].([]any)
	if !ok {
		return nil, fmt.Errorf("GeoJSON features must be a list, got %T", root["features"])
	}
	indexed := make(map[string]feature, len(items))
	for index, value := range items {
		item, err := decodeFeature(value, index)
		if err != nil {
			return nil, err
		}
		if _, duplicate := indexed[item.id]; duplicate {
			return nil, fmt.Errorf("GeoJSON features contain duplicate hotel ID %q", item.id)
		}
		indexed[item.id] = item
	}
	return indexed, nil
}

func decodeFeature(value any, index int) (feature, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return feature{}, fmt.Errorf("GeoJSON feature %d must be an object, got %T", index, value)
	}
	if err := httpcheck.ExactFields(object, "type", "id", "properties", "geometry"); err != nil {
		return feature{}, fmt.Errorf("GeoJSON feature %d: %w", index, err)
	}
	if object["type"] != "Feature" {
		return feature{}, fmt.Errorf("GeoJSON feature %d type = %v, expected Feature", index, object["type"])
	}
	id, ok := object["id"].(string)
	if !ok || id == "" {
		return feature{}, fmt.Errorf("GeoJSON feature %d id must be a non-empty string", index)
	}
	properties, ok := object["properties"].(map[string]any)
	if !ok {
		return feature{}, fmt.Errorf("GeoJSON feature %d properties must be an object", index)
	}
	if err := httpcheck.ExactFields(properties, "name", "phone_number"); err != nil {
		return feature{}, fmt.Errorf("GeoJSON feature %d properties: %w", index, err)
	}
	name, nameOK := properties["name"].(string)
	phone, phoneOK := properties["phone_number"].(string)
	if !nameOK || name == "" || !phoneOK || phone == "" {
		return feature{}, fmt.Errorf("GeoJSON feature %d name and phone_number must be non-empty strings", index)
	}
	geometry, ok := object["geometry"].(map[string]any)
	if !ok {
		return feature{}, fmt.Errorf("GeoJSON feature %d geometry must be an object", index)
	}
	if err := httpcheck.ExactFields(geometry, "type", "coordinates"); err != nil {
		return feature{}, fmt.Errorf("GeoJSON feature %d geometry: %w", index, err)
	}
	if geometry["type"] != "Point" {
		return feature{}, fmt.Errorf("GeoJSON feature %d geometry type = %v, expected Point", index, geometry["type"])
	}
	coordinates, ok := geometry["coordinates"].([]any)
	if !ok || len(coordinates) != 2 {
		return feature{}, fmt.Errorf("GeoJSON feature %d coordinates must contain exactly longitude and latitude", index)
	}
	lon, err := httpcheck.Number(coordinates[0])
	if err != nil {
		return feature{}, fmt.Errorf("GeoJSON feature %d longitude: %w", index, err)
	}
	lat, err := httpcheck.Number(coordinates[1])
	if err != nil {
		return feature{}, fmt.Errorf("GeoJSON feature %d latitude: %w", index, err)
	}
	return feature{id: id, name: name, phone: phone, lat: lat, lon: lon, raw: object}, nil
}

func validateProfiles(actual map[string]feature, catalog map[string]profile, where string) error {
	for id, item := range actual {
		expected, ok := catalog[id]
		if !ok {
			return fmt.Errorf("%s returned unknown hotel ID %q", where, id)
		}
		expectedFeature := map[string]any{
			"type": "Feature", "id": expected.id,
			"properties": map[string]any{"name": expected.name, "phone_number": expected.phone},
			"geometry": map[string]any{
				"type": "Point", "coordinates": []any{expected.lon, expected.lat},
			},
		}
		equal, err := jsoncheck.Equal(item.raw, expectedFeature)
		if err != nil {
			return fmt.Errorf("%s hotel %s: %w", where, id, err)
		}
		if !equal {
			return fmt.Errorf("%s hotel %s does not match the seeded profile: got %v", where, id, item.raw)
		}
	}
	return nil
}

func exactIDs(actual map[string]feature, expected ...string) error {
	wanted := make(map[string]struct{}, len(expected))
	for _, id := range expected {
		if _, duplicate := wanted[id]; duplicate {
			return fmt.Errorf("duplicate expected hotel ID %q", id)
		}
		wanted[id] = struct{}{}
	}
	for id := range wanted {
		if _, ok := actual[id]; !ok {
			return fmt.Errorf("missing hotel ID %q", id)
		}
	}
	for id := range actual {
		if _, ok := wanted[id]; !ok {
			return fmt.Errorf("unexpected hotel ID %q", id)
		}
	}
	return nil
}

func sameIDs(left, right map[string]feature) error {
	if len(left) != len(right) {
		return fmt.Errorf("hotel ID sets differ in size: first=%d second=%d", len(left), len(right))
	}
	for id := range left {
		if _, ok := right[id]; !ok {
			return fmt.Errorf("warm response omitted hotel ID %q from the first response", id)
		}
	}
	return nil
}
