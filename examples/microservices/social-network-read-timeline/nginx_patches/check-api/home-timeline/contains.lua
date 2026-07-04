local _M = {}
local k8s_suffix = os.getenv("fqdn_suffix")
if (k8s_suffix == nil) then k8s_suffix = "" end

function _M.Contains()
  local ngx = ngx
  local GenericObjectPool = require "GenericObjectPool"
  local social_network_HomeTimelineService = require "social_network_HomeTimelineService"
  local HomeTimelineServiceClient = social_network_HomeTimelineService.HomeTimelineServiceClient
  local bridge_tracer = require "opentracing_bridge_tracer"
  local liblualongnumber = require "liblualongnumber"

  local args = ngx.req.get_uri_args()
  local user_id = tonumber(args.user_id)
  local post_id_str = tostring(args.post_id or "")

  local req_id = tonumber(string.sub(ngx.var.request_id, 0, 15), 16)
  local tracer = bridge_tracer.new_from_global()
  local parent_span_context = tracer:binary_extract(ngx.var.opentracing_binary_context)
  local span = tracer:start_span("check_ht_contains", {["references"] = {{"child_of", parent_span_context}}})
  local carrier = {}
  tracer:text_map_inject(span:context(), carrier)

  local client = GenericObjectPool:connection(HomeTimelineServiceClient, "home-timeline-service" .. k8s_suffix, 9090)
  local status, ret = pcall(client.ReadHomeTimeline, client, req_id, user_id, 0, 200, carrier)

  if not status then
    pcall(function() client.iprot.trans:close() end)
    span:finish()
    ngx.header.content_type = "application/json; charset=utf-8"
    ngx.say('{"found":false}')
    ngx.exit(ngx.HTTP_OK)
  end

  GenericObjectPool:returnConnection(client)
  span:finish()

  local found = false
  for _, post in ipairs(ret) do
    if tostring(post.post_id) == post_id_str then
      found = true
      break
    end
  end

  ngx.header.content_type = "application/json; charset=utf-8"
  ngx.say('{"found":' .. tostring(found) .. '}')
  ngx.exit(ngx.HTTP_OK)
end

return _M
