# HTTP driver

This package implements the evaluator's HTTP protocol driver. It accepts
`api.HTTPRequestSpec` invocation payloads and returns `api.HTTPResponse`
payloads inside the common protocol result.

The driver handles URL resolution, query/form/body encoding, headers, response
metadata, transport error classification, and response-size limits. Target
session policy is explicit:

- `reuse` enables pooled connections and HTTP/2 negotiation where supported;
- `new_per_request` disables keep-alive to preserve scenarios whose historical
  benchmark included a fresh connection per request.

HTTP status is preserved as native status but is not interpreted as
application success here. The application adapter owns allowed-status and body
semantics.
