#!/bin/bash
# 部署示例: 把网页 + 实时查询后端发到你自己的一台 VPS。
# 这是模板, 不含任何真实主机信息; 复制成 deploy.sh 并按自己的环境填环境变量后使用。
#
# 用法:
#   export NDX_HOST=root@your-vps-ip
#   export NDX_SSH_KEY=~/.ssh/your_key
#   export NDX_DOMAIN=ndx.example.com
#   export NDX_WEB_ROOT=/var/www/ndx      # nginx 站点根目录, 放生成的 index.html
#   export NDX_API_ROOT=/opt/ndx         # api.py + fetch_ndx.py 运行目录
#   bash deploy.example.sh
set -e
cd "$(dirname "$0")"

: "${NDX_HOST:?需要设置 NDX_HOST, 例如 root@1.2.3.4}"
: "${NDX_SSH_KEY:?需要设置 NDX_SSH_KEY, 例如 ~/.ssh/id_ed25519}"
: "${NDX_DOMAIN:?需要设置 NDX_DOMAIN, 例如 ndx.example.com}"
: "${NDX_WEB_ROOT:=/var/www/ndx}"
: "${NDX_API_ROOT:=/opt/ndx}"

H="$NDX_HOST"; K="$NDX_SSH_KEY"

python3 web/build.py >/dev/null
python3 web/build.py --api "https://$NDX_DOMAIN" --live-url "https://$NDX_DOMAIN" --out dist/index_live.html >/dev/null

scp -i "$K" -q dist/index_live.html "$H:$NDX_WEB_ROOT/index.html"
scp -i "$K" -q web/template.html web/build.py "$H:$NDX_API_ROOT/web/"
scp -i "$K" -q scripts/fetch_ndx.py scripts/api.py "$H:$NDX_API_ROOT/"

ssh -i "$K" "$H" "cd $NDX_API_ROOT && systemctl restart ndx-api; sleep 1; systemctl is-active ndx-api"

L=$(md5 -q dist/index_live.html 2>/dev/null || md5sum dist/index_live.html | awk '{print $1}')
sleep 1
curl -s -o /tmp/ndx_live.html -w "线上 %{http_code}\n" "https://$NDX_DOMAIN/"
R=$(md5 -q /tmp/ndx_live.html 2>/dev/null || md5sum /tmp/ndx_live.html | awk '{print $1}')
[ "$R" = "$L" ] && echo "✅ 线上=本地 $L" || { echo "❌ 线上内容与本地不一致"; exit 1; }
curl -s "https://$NDX_DOMAIN/api/status"

# ---- systemd 单元示例 (放在 VPS /etc/systemd/system/ndx-api.service) ----
#
# [Unit]
# Description=NDX rebalance live query API
# After=network.target
#
# [Service]
# WorkingDirectory=/opt/ndx
# ExecStart=/opt/ndx/venv/bin/python3 api.py --port 8894
# Restart=always
# User=root
#
# [Install]
# WantedBy=multi-user.target
#
# 启用: systemctl daemon-reload && systemctl enable --now ndx-api

# ---- nginx 片段示例 (放在 /etc/nginx/sites-available/ndx.conf) ----
#
# server {
#     listen 443 ssl http2;
#     server_name ndx.example.com;
#     root /var/www/ndx;
#     index index.html;
#
#     location /api/ {
#         proxy_pass http://127.0.0.1:8894;
#         proxy_set_header Host $host;
#         limit_req zone=ndx_api burst=10 nodelay;
#     }
#
#     location / {
#         try_files $uri $uri/ =404;
#     }
#
#     ssl_certificate     /etc/letsencrypt/live/ndx.example.com/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/ndx.example.com/privkey.pem;
# }
#
# limit_req_zone $binary_remote_addr zone=ndx_api:10m rate=30r/m;  # 放在 http{} 块
