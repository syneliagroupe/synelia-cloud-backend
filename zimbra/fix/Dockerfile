# Not built/used by ../docker-compose.yml anymore (that now runs the stock
# iwayvietnam/zimbra_all image directly against an already-installed volume —
# see its "KNOWN GAP" comment). Kept as reference: build.sh below documents 4
# fresh-install fixes (search "Synelia fix") that a disaster-recovery
# reinstall on an empty volume would still need, none of which are ported
# into the current entrypoint. The FIPS one specifically:
#
# The bundled OpenSSL 3.0.9 forces `default_properties = fips=yes`, which
# makes zmsetup.pl's PKCS12 keystore export fail (PKCS12KDF isn't
# FIPS-approved) on a modern kernel/openssl combo. build.sh disables that
# forced default right before zmsetup.pl runs.
FROM iwayvietnam/zimbra_all:latest
COPY build.sh /opt/build.sh
