# vs-mcp

Leaf library for the pieces of an MCP tool server that several VibeSys servers
share. Import everything from `vs_mcp.api`.

- `ToolSpec` declares one tool: name, description, a Pydantic input schema and a
  handler.
- `serve_stdio` and `register_tool` run a FastMCP stdio server from a list of
  `ToolSpec` values. The server flattens each schema so the offered arguments
  match the model's fields.
- `ToolServerDescriptor`, `StdioServerDescriptor` and `expose_as_tools` describe
  how a host launches such a server as a subprocess, using primitives only.

It depends only on `pydantic` and `mcp`. How a host maps a descriptor onto an
agent's tool transport (sandbox paths, interpreter, scope) stays with that host,
for example `vs_agent`.
