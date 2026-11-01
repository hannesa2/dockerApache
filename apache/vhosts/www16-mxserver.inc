ProxyPreserveHost On
RedirectMatch 301 ^/backend$ /backend/
ProxyPass /backend/MXServer/ http://mxserver:8080/backend/MXServer/ connectiontimeout=5 timeout=120 retry=0
ProxyPassReverse /backend/MXServer/ http://mxserver:8080/backend/MXServer/
ProxyPass /backend/ http://mxserver:8080/backend/MXServer/ connectiontimeout=5 timeout=120 retry=0
ProxyPassReverse /backend/ http://mxserver:8080/backend/MXServer/
RequestHeader set X-Forwarded-Proto "https"
