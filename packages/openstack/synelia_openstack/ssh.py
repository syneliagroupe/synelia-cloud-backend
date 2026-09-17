"""SSH vers les VM de la zone VPS partagée (hébergement web) : écriture d'un service Docker
Compose et d'une route Traefik pour une application installée *après coup* sur une VM déjà
en service (cf. `web_hebergement.router_sites` — plusieurs sites/applications partagent une
même VM d'hébergement, contrairement à Drive qui obtient sa propre VM dédiée).

Paire `SshSimule` / `SshReel`, choisie par `fournisseur()` comme le reste de ce paquet — pas
de variable d'environnement dédiée, `web_hebergement` gate déjà tout sur `SYNELIA_FOURNISSEUR`.
Appels synchrones (paramiko), comme le reste des classes `*OpenStack` de ce paquet (openstacksdk
est lui aussi synchrone) : ce n'est pas idiomatique dans une base autrement `async def`, mais
c'est le patron déjà en place ici, pas une nouveauté."""

from __future__ import annotations

import io

from synelia_kernel.ids import nouvel_id


class SshSimule:
    def generer_cle(self) -> dict[str, str]:
        return {"prive": f"cle-simulee-{nouvel_id()}", "publique": f"ssh-ed25519 AAAA{nouvel_id()}"}

    def ecrire_fichier(
        self, hote: str, cle_privee: str, chemin: str, contenu: str, utilisateur: str = "root"
    ) -> None:
        return None

    def executer(
        self, hote: str, cle_privee: str, commande: str, utilisateur: str = "root"
    ) -> str:
        return ""


class SshReel(SshSimule):
    def generer_cle(self) -> dict[str, str]:
        import paramiko

        # RSA, pas Ed25519 : `Ed25519Key` (paramiko 5.x, lib PyNaCl) n'expose plus de
        # méthode `generate()` — seule `RSAKey.generate()` en offre une directement,
        # sans dépendance supplémentaire (RSA reste tout à fait adapté à un simple accès
        # SSH backend → VM interne, pas un usage exposé publiquement).
        cle = paramiko.RSAKey.generate(3072)
        tampon = io.StringIO()
        cle.write_private_key(tampon)
        publique = f"ssh-rsa {cle.get_base64()} synelia-hebergement"
        return {"prive": tampon.getvalue(), "publique": publique}

    def _client(self, hote: str, cle_privee: str, utilisateur: str):  # type: ignore[no-untyped-def]
        import paramiko

        cle = paramiko.RSAKey.from_private_key(io.StringIO(cle_privee))
        client = paramiko.SSHClient()
        # VM interne de la zone VPS, jamais exposée en dehors du réseau privé partagé : pas
        # d'infrastructure de clés d'hôte à vérifier, comme pour toute VM fraîchement créée
        # par Nova (même contournement que la connexion SSH manuelle vers ctrl1/comp1).
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507
        client.connect(hote, username=utilisateur, pkey=cle, timeout=20, banner_timeout=20)
        return client

    def ecrire_fichier(
        self, hote: str, cle_privee: str, chemin: str, contenu: str, utilisateur: str = "root"
    ) -> None:
        client = self._client(hote, cle_privee, utilisateur)
        try:
            dossier = chemin.rsplit("/", 1)[0]
            client.exec_command(f"mkdir -p {dossier}")
            sftp = client.open_sftp()
            try:
                with sftp.open(chemin, "w") as f:
                    f.write(contenu)
            finally:
                sftp.close()
        finally:
            client.close()

    def executer(
        self, hote: str, cle_privee: str, commande: str, utilisateur: str = "root"
    ) -> str:
        from synelia_kernel import erreurs

        client = self._client(hote, cle_privee, utilisateur)
        try:
            _stdin, stdout, stderr = client.exec_command(commande, timeout=120)
            code = stdout.channel.recv_exit_status()
            sortie = stdout.read().decode(errors="replace")
            erreur = stderr.read().decode(errors="replace")
            if code != 0:
                raise erreurs.amont_indisponible(
                    "hébergement (SSH)", f"`{commande}` a échoué ({code}) : {erreur[:300]}"
                )
            return sortie
        finally:
            client.close()
