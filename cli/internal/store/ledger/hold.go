package ledger

import (
	"fmt"
	"math"
	"reflect"
	"regexp"
	"time"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var timeType = reflect.TypeOf(time.Time{})

// Hold returns a deep copy of v in the one form every store keeps and returns, or a
// core.FailureInvalidInput naming the field that no store can hold, with what as the start of
// its path. The rules are in the documentation of store.Store.
//
// It walks the value by reflection, so a field added to a core type is covered without a change
// here. That is the point: an implementation that stored a value another would reject would make
// the in-memory store more forgiving than the on-disk one.
func Hold[T any](what string, v T) (T, error) {
	var out T
	held, err := hold(reflect.ValueOf(&v).Elem(), what)
	if err != nil {
		return out, err
	}
	reflect.ValueOf(&out).Elem().Set(held)
	return out, nil
}

// Copy returns the held form of a value that is already held. A held value cannot fail again,
// so a failure is a bug in this package.
func Copy[T any](v T) T {
	held, err := Hold("value", v)
	if err != nil {
		panic(fmt.Sprintf("ledger: a stored value is not holdable: %v", err))
	}
	return held
}

func invalid(path, reason string) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("%s: %s", path, reason)}
}

func hold(src reflect.Value, path string) (reflect.Value, error) {
	typ := src.Type()
	switch src.Kind() {
	case reflect.Struct:
		return holdStruct(src, path)
	case reflect.Pointer:
		if src.IsNil() {
			return src, nil
		}
		elem, err := hold(src.Elem(), path)
		if err != nil {
			return src, err
		}
		dst := reflect.New(typ.Elem())
		dst.Elem().Set(elem)
		return dst, nil
	case reflect.Interface:
		if src.IsNil() {
			return src, nil
		}
		inner := src.Elem()
		if inner.Kind() == reflect.Pointer {
			return src, invalid(path, fmt.Sprintf("holds a %s; it must hold a struct value", inner.Type()))
		}
		elem, err := hold(inner, path)
		if err != nil {
			return src, err
		}
		dst := reflect.New(typ).Elem()
		dst.Set(elem)
		return dst, nil
	case reflect.Slice:
		return holdSlice(src, path)
	case reflect.Map:
		return holdMap(src, path)
	case reflect.String:
		if !utf8.ValidString(src.String()) {
			return src, invalid(path, "is not valid UTF-8")
		}
	case reflect.Float32, reflect.Float64:
		if f := src.Float(); math.IsNaN(f) || math.IsInf(f, 0) {
			return src, invalid(path, "is not a finite number")
		}
	default:
	}
	return src, nil
}

func holdStruct(src reflect.Value, path string) (reflect.Value, error) {
	typ := src.Type()
	if typ == timeType {
		t := src.Interface().(time.Time).UTC()
		if year := t.Year(); year < 0 || year > 9999 {
			return src, invalid(path, fmt.Sprintf("year %d is outside 0 to 9999", year))
		}
		return reflect.ValueOf(t), nil
	}
	dst := reflect.New(typ).Elem()
	dst.Set(src)
	for i := range typ.NumField() {
		field := typ.Field(i)
		if !field.IsExported() {
			continue
		}
		held, err := hold(src.Field(i), path+"."+field.Name)
		if err != nil {
			return src, err
		}
		dst.Field(i).Set(held)
	}
	return dst, nil
}

func holdSlice(src reflect.Value, path string) (reflect.Value, error) {
	if src.Len() == 0 {
		return reflect.Zero(src.Type()), nil
	}
	dst := reflect.MakeSlice(src.Type(), src.Len(), src.Len())
	if src.Type().Elem().Kind() == reflect.Uint8 {
		reflect.Copy(dst, src)
		return dst, nil
	}
	for i := range src.Len() {
		held, err := hold(src.Index(i), fmt.Sprintf("%s[%d]", path, i))
		if err != nil {
			return src, err
		}
		dst.Index(i).Set(held)
	}
	return dst, nil
}

func holdMap(src reflect.Value, path string) (reflect.Value, error) {
	if src.Len() == 0 {
		return reflect.Zero(src.Type()), nil
	}
	dst := reflect.MakeMapWithSize(src.Type(), src.Len())
	iter := src.MapRange()
	for iter.Next() {
		keyPath := fmt.Sprintf("%s[%q]", path, iter.Key())
		key, err := hold(iter.Key(), keyPath)
		if err != nil {
			return src, err
		}
		value, err := hold(iter.Value(), keyPath)
		if err != nil {
			return src, err
		}
		dst.SetMapIndex(key, value)
	}
	return dst, nil
}

var namePattern = regexp.MustCompile(`^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$`)

// CheckName fails with core.FailureInvalidInput unless s is a name: 1 to 128 of A-Z, a-z, 0-9,
// dot, underscore and hyphen, not starting with a dot. what describes s in the message.
func CheckName(what, s string) error {
	if !namePattern.MatchString(s) {
		return invalid(what, fmt.Sprintf("%q is not a name of 1 to 128 letters, digits, dots, underscores and hyphens that does not start with a dot", s))
	}
	return nil
}
