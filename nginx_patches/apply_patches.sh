#!/bin/bash
set -e

NGINX_CONTAINER="socialnetwork-nginx-thrift-1"
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Applying nginx_patches to $NGINX_CONTAINER ..."

docker exec "$NGINX_CONTAINER" mkdir -p \
  /usr/local/openresty/nginx/lua-scripts/check-api/social-graph \
  /usr/local/openresty/nginx/lua-scripts/check-api/user-timeline \
  /usr/local/openresty/nginx/lua-scripts/check-api/home-timeline

docker cp "$PATCH_DIR/check-api/social-graph/get_followees.lua" \
  "$NGINX_CONTAINER:/usr/local/openresty/nginx/lua-scripts/check-api/social-graph/get_followees.lua"
docker cp "$PATCH_DIR/check-api/user-timeline/contains.lua" \
  "$NGINX_CONTAINER:/usr/local/openresty/nginx/lua-scripts/check-api/user-timeline/contains.lua"
docker cp "$PATCH_DIR/check-api/home-timeline/contains.lua" \
  "$NGINX_CONTAINER:/usr/local/openresty/nginx/lua-scripts/check-api/home-timeline/contains.lua"

docker cp "$PATCH_DIR/timed-scripts/compose.lua" \
  "$NGINX_CONTAINER:/usr/local/openresty/nginx/lua-scripts/wrk2-api/post/compose.lua"
docker cp "$PATCH_DIR/timed-scripts/user_timeline_read.lua" \
  "$NGINX_CONTAINER:/usr/local/openresty/nginx/lua-scripts/wrk2-api/user-timeline/read.lua"
docker cp "$PATCH_DIR/timed-scripts/home_timeline_read.lua" \
  "$NGINX_CONTAINER:/usr/local/openresty/nginx/lua-scripts/wrk2-api/home-timeline/read.lua"

docker cp "$NGINX_CONTAINER:/usr/local/openresty/nginx/conf/nginx.conf" /tmp/nginx.conf.pulled

python3 - "$PATCH_DIR/nginx_check_api_blocks.conf" << 'PYEOF'
import sys
blocks_path = sys.argv[1]
with open('/tmp/nginx.conf.pulled') as f:
    content = f.read()
with open(blocks_path) as f:
    blocks = f.read()

if '/check-api/social-graph/get_followees' not in content:
    marker = 'location /wrk2-api/home-timeline/read'
    content = content.replace(marker, blocks.strip('\n') + '\n\n    ' + marker)

with open('/tmp/nginx.conf.patched', 'w') as f:
    f.write(content)

print("check-api blocks present:", '/check-api/social-graph/get_followees' in content)
PYEOF

docker cp /tmp/nginx.conf.patched "$NGINX_CONTAINER:/usr/local/openresty/nginx/conf/nginx.conf.new"
docker exec "$NGINX_CONTAINER" cp \
  /usr/local/openresty/nginx/conf/nginx.conf.new \
  /usr/local/openresty/nginx/conf/nginx.conf

docker exec "$NGINX_CONTAINER" /usr/local/openresty/nginx/sbin/nginx -t
docker exec "$NGINX_CONTAINER" /usr/local/openresty/nginx/sbin/nginx -s reload

echo "Patches applied and nginx reloaded."
echo "Verifying..."
sleep 2

curl -s "http://localhost:8080/check-api/social-graph/get_followees?user_id=1" && echo " <- get_followees OK"
