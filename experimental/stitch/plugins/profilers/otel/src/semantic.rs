//! `OTel` semantic conventions used to name and classify spans. The rules
//! match `semanticOperation`, `normalizeSpanKind` and `spanFailed` in the
//! evaluator's telemetry package, so both produce the same aggregation keys.

use crate::{AttributeValue, Attributes};
use stitch_core::SpanKind;

fn text<'a>(attributes: &'a Attributes, key: &str) -> Option<&'a str> {
    match attributes.get(key) {
        Some(AttributeValue::Text(value)) if !value.is_empty() => Some(value),
        _ => None,
    }
}

pub(crate) fn first_text(attributes: &Attributes, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| text(attributes, key))
        .map(str::to_owned)
}

/// A low-cardinality operation: the HTTP route, the RPC service and method,
/// the database system and operation, or else the span name without its query
/// string.
pub(crate) fn operation(name: &str, attributes: &Attributes) -> String {
    if let Some(route) = text(attributes, "http.route") {
        return match text(attributes, "http.request.method") {
            Some(method) => format!("{method} {route}"),
            None => route.to_owned(),
        };
    }
    if let (Some(service), Some(method)) = (
        text(attributes, "rpc.service"),
        text(attributes, "rpc.method"),
    ) {
        return format!("{service}/{method}");
    }
    if let Some(system) = first_text(attributes, &["db.system.name", "db.system"]) {
        if let Some(operation) = text(attributes, "db.operation.name") {
            return format!("{system}:{operation}");
        }
    }
    if name.is_empty() {
        return "unknown".into();
    }
    strip_query_string(name)
}

fn strip_query_string(value: &str) -> String {
    let Some(query) = value.find('?') else {
        return value.to_owned();
    };
    match value[query..].find([' ', '\t']) {
        Some(suffix) => format!("{}{}", &value[..query], &value[query + suffix..]),
        None => value[..query].to_owned(),
    }
}

/// Accept both the OTLP JSON integer encoding and enum names.
pub(crate) fn span_kind(raw: &serde_json::Value) -> SpanKind {
    let name = match raw {
        serde_json::Value::Number(number) => number.to_string(),
        serde_json::Value::String(text) => text.to_ascii_lowercase(),
        _ => String::new(),
    };
    match name.trim_start_matches("span_kind_") {
        "2" | "server" => SpanKind::Server,
        "3" | "client" => SpanKind::Client,
        "4" | "producer" => SpanKind::Producer,
        "5" | "consumer" => SpanKind::Consumer,
        _ => SpanKind::Internal,
    }
}

pub(crate) fn failed(status_code: &str, attributes: &Attributes) -> bool {
    let code = status_code.to_ascii_uppercase();
    code == "2"
        || code == "STATUS_CODE_ERROR"
        || text(attributes, "error.type").is_some()
        || text(attributes, "exception.type").is_some()
        || attributes.get("error") == Some(&AttributeValue::Bool(true))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn attrs(pairs: &[(&str, &str)]) -> Attributes {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), AttributeValue::Text((*v).to_owned())))
            .collect()
    }

    #[test]
    fn operation_prefers_route_then_rpc_then_database_then_name() {
        assert_eq!(
            operation(
                "GET",
                &attrs(&[("http.route", "/hotels"), ("http.request.method", "GET")])
            ),
            "GET /hotels"
        );
        assert_eq!(
            operation("x", &attrs(&[("http.route", "/hotels")])),
            "/hotels"
        );
        assert_eq!(
            operation(
                "x",
                &attrs(&[("rpc.service", "search.Search"), ("rpc.method", "Nearby")])
            ),
            "search.Search/Nearby"
        );
        assert_eq!(
            operation(
                "x",
                &attrs(&[("db.system", "mongodb"), ("db.operation.name", "find")])
            ),
            "mongodb:find"
        );
        assert_eq!(
            operation("mongo find", &attrs(&[("db.system", "mongodb")])),
            "mongo find"
        );
        assert_eq!(
            operation("GET /hotels?in=1 HTTP", &attrs(&[])),
            "GET /hotels HTTP"
        );
        assert_eq!(operation("GET /hotels?in=1", &attrs(&[])), "GET /hotels");
        assert_eq!(operation("", &attrs(&[])), "unknown");
    }

    #[test]
    fn span_kind_accepts_integer_and_enum_encodings() {
        assert_eq!(span_kind(&serde_json::json!(2)), SpanKind::Server);
        assert_eq!(span_kind(&serde_json::json!(3)), SpanKind::Client);
        assert_eq!(
            span_kind(&serde_json::json!("SPAN_KIND_CLIENT")),
            SpanKind::Client
        );
        assert_eq!(
            span_kind(&serde_json::json!("consumer")),
            SpanKind::Consumer
        );
        assert_eq!(span_kind(&serde_json::json!(0)), SpanKind::Internal);
        assert_eq!(span_kind(&serde_json::Value::Null), SpanKind::Internal);
    }

    #[test]
    fn failure_comes_from_status_or_error_attributes() {
        assert!(failed("2", &attrs(&[])));
        assert!(failed("STATUS_CODE_ERROR", &attrs(&[])));
        assert!(!failed("STATUS_CODE_OK", &attrs(&[])));
        assert!(failed("", &attrs(&[("error.type", "timeout")])));
        let mut flagged = Attributes::new();
        flagged.insert("error".into(), AttributeValue::Bool(true));
        assert!(failed("", &flagged));
    }
}
