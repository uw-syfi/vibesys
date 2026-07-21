package jsoncheck

import (
	"encoding/json"
	"fmt"
	"math/big"
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
	if err := c.ValidateDefinition(); err != nil {
		return nil, err
	}
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
	if _, err := c.Key(object); err != nil {
		return nil, fmt.Errorf("%s key: %w", where, err)
	}
	return object, nil
}

func (c Contract) ValidateDefinition() error {
	if c.Name == "" {
		return fmt.Errorf("JSON contract name must not be empty")
	}
	if len(c.Fields) == 0 {
		return fmt.Errorf("%s contract declares no fields", c.Name)
	}
	for field, validate := range c.Fields {
		if field == "" {
			return fmt.Errorf("%s contract declares an empty field name", c.Name)
		}
		if validate == nil {
			return fmt.Errorf("%s contract field %q has no validator", c.Name, field)
		}
	}
	if c.Key == nil {
		return fmt.Errorf("%s contract has no key function", c.Name)
	}
	return nil
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

// ExactList validates every actual and expected row, rejects duplicate keys,
// and requires exact key membership and values. Adapters should prefer this to
// presence-only list checks whenever they own the complete expected state.
func (c Contract) ExactList(
	actualValue any,
	expectedValues []any,
	where string,
) (map[string]Object, error) {
	actual, err := c.IndexList(actualValue, where)
	if err != nil {
		return nil, err
	}
	expected := make(map[string]Object, len(expectedValues))
	for index, value := range expectedValues {
		normalized, err := Normalize(value)
		if err != nil {
			return nil, fmt.Errorf("%s expected item %d: %w", where, index, err)
		}
		object, err := c.Validate(normalized, fmt.Sprintf("%s expected item %d", where, index))
		if err != nil {
			return nil, err
		}
		key, err := c.Key(object)
		if err != nil {
			return nil, fmt.Errorf("%s expected item %d key: %w", where, index, err)
		}
		if _, duplicate := expected[key]; duplicate {
			return nil, fmt.Errorf("%s: duplicate expected key %q at item %d", where, key, index)
		}
		expected[key] = object
	}
	for key, expectedObject := range expected {
		actualObject, exists := actual[key]
		if !exists {
			return nil, fmt.Errorf("%s: missing key %q", where, key)
		}
		if !equalJSON(actualObject, expectedObject) {
			return nil, fmt.Errorf(
				"%s: value mismatch for key %q: got %v, want %v",
				where,
				key,
				actualObject,
				expectedObject,
			)
		}
	}
	for key := range actual {
		if _, exists := expected[key]; !exists {
			return nil, fmt.Errorf("%s: unexpected key %q", where, key)
		}
	}
	return actual, nil
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
	return equalJSON(actual, normalized), nil
}

func equalJSON(left, right any) bool {
	leftNumber, leftIsNumber := left.(json.Number)
	rightNumber, rightIsNumber := right.(json.Number)
	if leftIsNumber || rightIsNumber {
		if !leftIsNumber || !rightIsNumber {
			return false
		}
		leftRational, leftOK := new(big.Rat).SetString(string(leftNumber))
		rightRational, rightOK := new(big.Rat).SetString(string(rightNumber))
		return leftOK && rightOK && leftRational.Cmp(rightRational) == 0
	}
	leftObject, leftIsObject := left.(map[string]any)
	rightObject, rightIsObject := right.(map[string]any)
	if leftIsObject || rightIsObject {
		if !leftIsObject || !rightIsObject || len(leftObject) != len(rightObject) {
			return false
		}
		for key, leftValue := range leftObject {
			rightValue, exists := rightObject[key]
			if !exists || !equalJSON(leftValue, rightValue) {
				return false
			}
		}
		return true
	}
	leftList, leftIsList := left.([]any)
	rightList, rightIsList := right.([]any)
	if leftIsList || rightIsList {
		if !leftIsList || !rightIsList || len(leftList) != len(rightList) {
			return false
		}
		for index := range leftList {
			if !equalJSON(leftList[index], rightList[index]) {
				return false
			}
		}
		return true
	}
	return reflect.DeepEqual(left, right)
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
