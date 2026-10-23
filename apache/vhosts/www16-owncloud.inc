ProxyPreserveHost On
ProxyPass /owncloud/ http://owncloud:8080/owncloud/ connectiontimeout=5 timeout=3600 retry=0
ProxyPassReverse /owncloud/ http://owncloud:8080/owncloud/
RequestHeader set X-Forwarded-Proto expr=%{REQUEST_SCHEME}
