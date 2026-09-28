"""Cloud-init pour la passerelle OpenVPN d'un Espace Cloud (accès nomade)."""


def construire_cloud_init(cidr_espace: str, cle_publique_ssh: str) -> str:
    """Installe OpenVPN + easy-rsa, pousse la route vers `cidr_espace`, expose un script d'émission
    de profils client (`synelia-vpn-issue-client`) invoqué par l'API via SSH."""
    script_setup = f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y openvpn easy-rsa iptables-persistent
make-cadir /etc/openvpn/easy-rsa
cd /etc/openvpn/easy-rsa
./easyrsa init-pki
EASYRSA_BATCH=1 ./easyrsa build-ca nopass
EASYRSA_BATCH=1 ./easyrsa build-server-full server nopass
EASYRSA_BATCH=1 ./easyrsa gen-dh
openvpn --genkey secret /etc/openvpn/ta.key
mkdir -p /etc/openvpn/server
cat >/etc/openvpn/server/server.conf <<OVPN
port 1194
proto udp
dev tun
ca /etc/openvpn/easy-rsa/pki/ca.crt
cert /etc/openvpn/easy-rsa/pki/issued/server.crt
key /etc/openvpn/easy-rsa/pki/private/server.key
dh /etc/openvpn/easy-rsa/pki/dh.pem
tls-auth /etc/openvpn/ta.key 0
server 10.8.0.0 255.255.255.0
push "route {cidr_espace}"
keepalive 10 120
cipher AES-256-GCM
user nobody
group nogroup
persist-key
persist-tun
status /var/log/openvpn-status.log
verb 3
OVPN
systemctl enable openvpn-server@server
systemctl restart openvpn-server@server
echo 1 > /proc/sys/net/ipv4/ip_forward
iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o eth0 -j MASQUERADE
iptables-save >/etc/iptables/rules.v4
"""
    script_issue = """#!/bin/bash
set -euo pipefail
NOM="${1:?nom client requis}"
cd /etc/openvpn/easy-rsa
if [[ ! -f "pki/issued/${NOM}.crt" ]]; then
  EASYRSA_BATCH=1 ./easyrsa build-client-full "$NOM" nopass >/dev/null
fi
REMOTE="${SYNELIA_VPN_PUBLIC_HOST:?}"
PORT="${SYNELIA_VPN_PUBLIC_PORT:-1194}"
cat <<CFG
client
dev tun
proto udp
remote ${REMOTE} ${PORT}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher AES-256-GCM
verb 3
<ca>
$(cat pki/ca.crt)
</ca>
<cert>
$(cat pki/issued/${NOM}.crt)
</cert>
<key>
$(cat pki/private/${NOM}.key)
</key>
<tls-auth>
$(cat /etc/openvpn/ta.key)
</tls-auth>
key-direction 1
CFG
"""
    return (
        "#cloud-config\n"
        "disable_root: false\n"
        f"ssh_authorized_keys:\n  - {cle_publique_ssh}\n"
        "runcmd:\n"
        "  - bash /var/lib/cloud/instance/scripts/part-001\n"
        "write_files:\n"
        "  - path: /var/lib/cloud/instance/scripts/part-001\n"
        "    permissions: '0755'\n"
        "    content: |\n"
        + "".join(f"      {ligne}\n" for ligne in script_setup.splitlines())
        + "  - path: /usr/local/bin/synelia-vpn-issue-client\n"
        "    permissions: '0755'\n"
        "    content: |\n" + "".join(f"      {ligne}\n" for ligne in script_issue.splitlines())
    )
