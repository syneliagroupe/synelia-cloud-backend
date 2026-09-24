# Zimbra httpd fails to start: `libphp7.so` module mismatch

## Problem
On every `zimbra-all` container start, `httpd` (Apache, front-end for the
`zimbraAdmin`/spam-training webapps on port 8080→80) fails:
```
Starting apache...httpd: Syntax error on line 148 of /opt/zimbra/conf/httpd.conf:
Cannot load /opt/zimbra/common/lib/apache2/modules/libphp7.so into server:
/opt/zimbra/common/lib/apache2/modules/libphp7.so: cannot open shared object file: No such file or directory
```
The stock `iwayvietnam/zimbra_all` image ships a newer PHP build
(`libphp.so`, exporting symbol `php_module`) but `httpd.conf` (from an older
install) still has:
```
LoadModule php7_module        /opt/zimbra/common/lib/apache2/modules/libphp7.so
```
Not the same gap as `zimbra/fix/build.sh` (FIPS/jetty/amavis/JDK17) — this one
is a naming drift between the persisted `httpd.conf` and the image's PHP
build, unrelated to the fresh-install fixes.

Non-blocking for mail flow (postfix/submission on 587, `nginx` on 8443 serving
the actual webmail, both independent of `httpd`) — but blocks the
`zimbraAdmin`/spam-training webapp path on 8080 and leaves a permanent error
in `docker logs zimbra-all` on every restart.

## Fix (persisted — `/opt/zimbra` is bind-mounted from `zimbra/zimbra-storage`)
```bash
# edit the persisted config, not a symlink (the .so exports php_module, not php7_module)
sed -i 's/LoadModule php7_module.*libphp7.so/LoadModule php_module        \/opt\/zimbra\/common\/lib\/apache2\/modules\/libphp.so/' \
  zimbra/zimbra-storage/conf/httpd.conf
docker exec -u zimbra zimbra-all /opt/zimbra/bin/zmapachectl restart
```

## Verification
```bash
docker exec zimbra-all curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/   # 302, was connection refused
docker logs zimbra-all --tail 20   # no more "Syntax error on line 148"
```
