# Rebuilds iwayvietnam/zimbra_all with one fix: the bundled OpenSSL 3.0.9 forces
# `default_properties = fips=yes`, which makes zmsetup.pl's PKCS12 keystore export
# fail (PKCS12KDF isn't FIPS-approved) on a modern kernel/openssl combo. build.sh
# now disables that forced default right before zmsetup.pl runs.
FROM iwayvietnam/zimbra_all:latest
COPY build.sh /opt/build.sh
