# Zimbra relayhost persistence (zmconfigd wipe)

## Problem
`postconf -e relayhost` via `main.cf` is ephemeral — `zmconfigd` rewrites `main.cf` from LDAP every `zmconfigd_interval` and on restart (`zmconfigd.cf:131,224`). Hand edits disappear.

## Canonical fix (LDAP, persisted in volume)
```bash
# inside zimbra-all container as zimbra user
su - zimbra -c "zmprov mcf zimbraMtaRelayHost '[zimbra.smile.ci]:587' zimbraMtaSmtpSaslAuthEnable yes zimbraMtaSmtpSaslPasswordMaps lmdb:/opt/zimbra/conf/relay_password zimbraMtaSmtpSaslSecurityOptions noanonymous zimbraMtaSmtpTlsSecurityLevel may"
su - zimbra -c "echo '[zimbra.smile.ci]:587 cloud@synelia.tech:YOUR_SMTP_PASSWORD' > /opt/zimbra/conf/relay_password"
su - zimbra -c "postmap /opt/zimbra/conf/relay_password && zmcontrol restart && zmprov gcf zimbraMtaRelayHost && postconf relayhost"
# Verify
su - zimbra -c "zmprov gcf zimbraMtaRelayHost"  # should show [zimbra.smile.ci]:587
su - zimbra -c "postconf relayhost"
```

Volume `./zimbra-storage:/opt/zimbra:rw` already persists LDAP (`zimbra-ldap` data) — no file mount needed.

## Post-start hook (optional)
Add to `zimbra/docker-compose.yml` entrypoint after UID bootstrap:
```bash
# after rm -rf /opt/zimbra-install branch, before zmcontrol restart
if [ -x /opt/zimbra/bin/zmprov ]; then
  su - zimbra -c "zmprov gcf zimbraMtaRelayHost | grep -q zimbra.smile.ci || zmprov mcf zimbraMtaRelayHost '[zimbra.smile.ci]:587' ..."
fi
```

## Fresh install gap
`zimbra/fix/build.sh` 4 fixes (FIPS openssl.cnf.dist, jetty setuid, amavis StartTLS, JDK17 --add-opens) are not ported to `docker-compose.yml` stock image path — keep `zimbra/fix/` as reference for disaster recovery.

## Verification
```bash
swaks --server 127.0.0.1 --port 587 --tls --auth PLAIN --auth-user cloud@synelia.tech --to test@agentmail.to
curl -s http://127.0.0.1:4000/v1/web/smtp/webhooks/xxx/test | jq
```
