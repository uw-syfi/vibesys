package jsoncheck

import (
	"encoding/json"
	"fmt"
	"reflect"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
)

type Object = map[string]any
type FieldValidator func(any) error
type KeyFunc func(Object) (string, error)

type Contract struct {
	Name           string
	Fields         map[string]FieldValidator
	Key            KeyFunc
	ValidateObject func(Object) error
}

func (c Contract) Validate(value any, where string) (Object, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s: expected object, got %T", where, value)
	}
	fields := make([]string, 0, len(c.Fields))
	for field := range c.Fields {
		fields = append(fields, field)
	}
	if err := httpcheck.ExactFields(object, fields...); err != nil {
		return nil, fmt.Errorf("%s: %w", where, err)
	}
	for field, validate := range c.Fields {
		if err := validate(object[field]); err != nil {
			return nil, fmt.Errorf("%s.%s: %w", where, field, err)
		}
	}
	if c.ValidateObject != nil {
		if err := c.ValidateObject(object); err != nil {
			return nil, fmt.Errorf("%s: %w", where, err)
		}
	}
	if c.Key == nil {
		return nil, fmt.Errorf("%s contract has no key function", c.Name)
	}
	if _, err := c.Key(object); err != nil {
		return nil, fmt.Errorf("%s key: %w", where, err)
	}
	return object, nil
}

func (c Contract) IndexList(value any, where string) (map[string]Object, error) {
	items, ok := value.([]any)
	if !ok {
		return nil, fmt.Errorf("%s: expected list, got %T", where, value)
	}
	indexed := make(map[string]Object, len(items))
	for index, item := range items {
		object, err := c.Validate(item, fmt.Sprintf("%s item %d", where, index))
		if err != nil {
			return nil, err
		}
		key, err := c.Key(object)
		if err != nil {
			return nil, fmt.Errorf("%s item %d key: %w", where, index, err)
		}
		if _, duplicate := indexed[key]; duplicate {
			return nil, fmt.Errorf("%s: duplicate key %q at item %d", where, key, index)
		}
		indexed[key] = object
	}
	return indexed, nil
}

func Normalize(value any) (any, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("marshal expected JSON: %w", err)
	}
	return httpcheck.DecodeJSON(encoded)
}

func Equal(actual, expected any) (bool, error) {
	normalized, err := Normalize(expected)
	if err != nil {
		return false, err
	}
	return reflect.DeepEqual(actual, normalized), nil
}

func String(value any) error {
	if _, ok := value.(string); !ok {
		return fmt.Errorf("expected string, got %T", value)
	}
	return nil
}

func NonEmptyString(value any) error {
	text, ok := value.(string)
	if !ok || text == "" {
		return fmt.Errorf("expected non-empty string, got %v", value)
	}
	return nil
}

func Integer(value any) error {
	_, err := httpcheck.Integer(value)
	return err
}

func Number(value any) error {
	_, err := httpcheck.Number(value)
	return err
}

func StringList(value any) error {
	items, ok := value.([]any)
	if !ok {
		return fmt.Errorf("expected list, got %T", value)
	}
	for index, item := range items {
		if err := String(item); err != nil {
			return fmt.Errorf("item %d: %w", index, err)
		}
	}
	return nil
}

func IntegerList(value any) error {
	items, ok := value.([]any)
	if !ok {
		return fmt.Errorf("expected list, got %T", value)
	}
	for index, item := range items {
		if err := Integer(item); err != nil {
			return fmt.Errorf("item %d: %w", index, err)
		}
	}
	return nil
}

func ExactObject(fields map[string]FieldValidator) FieldValidator {
	return func(value any) error {
		object, ok := value.(map[string]any)
		if !ok {
			return fmt.Errorf("expected object, got %T", value)
		}
		names := make([]string, 0, len(fields))
		for name := range fields {
			names = append(names, name)
		}
		if err := httpcheck.ExactFields(object, names...); err != nil {
			return err
		}
		for name, validate := range fields {
			if err := validate(object[name]); err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
		}
		return nil
	}
}
