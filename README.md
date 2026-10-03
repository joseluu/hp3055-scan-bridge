# hp3055-scan-bridge

Fait fonctionner le bouton **"Scan" du panneau de controle** d'un HP LaserJet
3050/3052/3055/3390/3392 vers n'importe quel outil de scan sur reseau local (testé avec
[scanservjs](https://github.com/sbs20/scanservjs)), **sans** le logiciel HP "Full Solution"
d'epoque ni la VM Windows 2000 qu'il faut habituellement pour le faire tourner.

Ces imprimantes tout-en-un supportent le "scan to PC" declenche depuis leur panneau physique,
mais ce mecanisme n'a jamais ete documente publiquement par HP et n'a rien a voir avec un
scan reseau classique (SANE/WSD/AirScan) : c'est un protocole proprietaire, moitie HTTP/XML,
moitie SNMP, entierement reverse-engineere ici. Voir [`PROTOCOL.md`](PROTOCOL.md) pour le
detail complet (requetes exactes, OID SNMP, structure des trames).

## Comment ça marche

1. Le script s'enregistre aupres de l'imprimante comme plusieurs "destinations" de scan —
   une par profil couleur/resolution (elles apparaissent toutes dans la liste du panneau,
   l'imprimante n'ayant pas de bouton pour choisir le mode, c'est le choix de destination qui
   en tient lieu).
2. Il surveille en continu si l'une de ces destinations a ete choisie et le bouton "Scan"
   presse.
3. Quand c'est le cas, il envoie la sequence SNMP du profil correspondant, qui declenche
   reellement le moteur de scan (le simple polling HTTP ne suffit pas — c'est la piece
   manquante qui a pris le plus de temps a trouver).
4. Il recupere l'image scannee (JPEG) sur un port TCP dedie, la convertit selon le mode du
   profil (couleur / niveaux de gris / noir-et-blanc par seuillage logiciel) et la sauvegarde
   en PDF.

## Prerequis

- Python 3 + [Pillow](https://pypi.org/project/Pillow/) (`pip install pillow` ou paquet
  systeme `python3-pil`)
- `snmpset`/`snmpget` (paquet `snmp` sur Debian/Ubuntu, `net-snmp` ailleurs)
- Etre sur le meme reseau local que l'imprimante (protocole non authentifie, prevu pour du
  LAN de confiance)

## Configuration

Les parametres de connexion se reglent via variables d'environnement :

| Variable | Defaut | Description |
|---|---|---|
| `PRINTER_IP` | `192.168.1.2` | IP de l'imprimante |
| `HOST_ID` | `MYPC` | Doit correspondre au **nom NetBIOS reel** de la machine qui lance le script (l'imprimante semble le resoudre par NBNS avant d'accepter le scan) |
| `OUTPUT_DIR` | `./output` | Dossier ou sont deposes les PDF generes |
| `SNMP_COMMUNITY` | `internal` | Communaute SNMP (valeur observee sur le modele teste) |

```bash
export PRINTER_IP=192.168.1.50
export HOST_ID=$(hostname | tr a-z A-Z)
export OUTPUT_DIR=/srv/scans
python3 hp3055_scan_bridge.py
```

### Profils de scan (couleur/resolution/mode)

Le script enregistre **plusieurs destinations**, une par profil defini dans la constante
`PROFILES` en tete du script — le panneau de l'imprimante n'ayant pas de selecteur de mode,
c'est le choix de la destination qui en tient lieu. Chaque destination apparait au panneau
sous le nom `<HOST_ID>:<suffix>` (le panneau du 3055 n'affiche que 9 caracteres visibles
apres le HostID, d'ou des suffixes courts).

Profils fournis par defaut (valeurs SNMP calibrees et testees sur un HP LaserJet 3055, voir
`PROTOCOL.md` pour le detail des essais) :

| Suffixe | Mode | Resolution |
|---|---|---|
| `COLOR200` | Couleur | 200 dpi |
| `GRAY300` | Niveaux de gris | 300 dpi |
| `BW300` | Noir et blanc (seuillage logiciel) | 300 dpi |

L'imprimante ne distingue que 2 modes materiels (`.3.0` = `8` niveaux de gris / `24`
couleur) — il n'existe pas de 3e reglage materiel pour le "noir et blanc" : le profil
`BW300` recoit la meme image en niveaux de gris que `GRAY300`, mais le script la seuille
lui-meme en 1-bit avant de l'enregistrer (constante `BW_THRESHOLD`, 128 par defaut).

Pour ajouter/modifier un profil, editer la liste `PROFILES` dans le script — ce ne sont pas
des parametres libres mais des combinaisons calibrees d'apres des captures reseau reelles
(voir `PROTOCOL.md` pour les valeurs confirmees a d'autres resolutions).

Le script tourne en boucle indefiniment (`Ctrl+C` pour arreter), et journalise chaque etape
sur stdout. Pour un fonctionnement permanent, en faire un service systemd (`Restart=always`).

## Limitations connues

- Seuls 3 profils couleur/resolution sont calibres par defaut (voir ci-dessus) ; en ajouter
  d'autres necessite d'editer le script, pas seulement une variable d'environnement.
- Le niveau de compression JPEG (taille de fichier) n'est pas pilotable : confirme pilote par
  l'imprimante elle-meme mais via un mecanisme encore non identifie (voir `PROTOCOL.md`).
- Non teste avec le bac d'alimentation automatique (ADF) multi-pages.
- Testé sur un HP LaserJet 3055 ; les autres modeles de la meme gamme partagent
  vraisemblablement le meme firmware/moteur, mais ce n'est pas confirme.
- Un `DestinationID` de slot 0 a parfois semble en echec pendant le developpement ; le script
  sacrifie ce slot par precaution, mais la cause reelle n'a pas ete confirmee avec certitude
  (voir `PROTOCOL.md`).

## Licence

Domaine public / usage libre — protocole issu d'une retro-ingenierie a but d'interoperabilite
sur du materiel personnel. Aucune garantie.
