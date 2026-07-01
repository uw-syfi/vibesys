local _M = {}
local k8s_suffix = os.getenv("fqdn_suffix")
if (k8s_suffix == nil) then k8s_suffix = "" end

function _M.GetFollowees()
  local ngx = ngx
  local GenericObjectPool = require "GenericObjectPool"
  local SocialGraphServiceClient = require "social_network_SocialGraphService".SocialGraphServiceClient
  local cjson = require "cjson"
  local bridge_tracer = require "opentracing_bridge_tracer"
  local liblualongnumber = require "liblualongnumber"

  local args = ngx.req.get_uri_args()
  local user_id = tonumber(args.user_id)
  if user_id == nil then
    ngx.status = ngx.HTTP_BAD_REQUEST
    ngx.say('{"error":"missing user_id"}')
    ngx.exit(ngx.HTTP_BAD_REQUEST)
  end

  local req_id = tonumber(string.sub(ngx.var.request_id, 0, 15), 16)
  local tracer = bridge_tracer.new_from_global()
  local parent_span_context = tracer:binary_extract(ngx.var.opentracing_binary_context)
  local span = tracer:start_span("check_get_followees", {["references"] = {{"child_of", parent_span_context}}})
  local carrier = {}
  tracer:text_map_inject(span:context(), carrier)

  local client = GenericObjectPool:connection(SocialGraphServiceClient, "social-graph-service" .. k8s_suffix, 9090)
  local status, ret = pcall(client.GetFollowees, client, req_id, user_id, carrier)

  if not status then
    pcall(function() client.iprot.trans:close() end)
    span:finish()
    ngx.header.content_type = "application/json; charset=utf-8"
    ngx.say("[]")
    ngx.exit(ngx.HTTP_OK)
  end

  GenericObjectPool:returnConnection(client)
  span:finish()

  local followee_list = {}
  for _, fid in ipairs(ret) do
    table.insert(followee_list, tostring(fid))
  end

  ngx.header.content_type = "application/json; charset=utf-8"
  ngx.say(cjson.encode(followee_list))
  ngx.exit(ngx.HTTP_OK)
end

return _M
