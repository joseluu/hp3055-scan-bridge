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

1. Le script s'enregistre aupres de l'imprimante comme "destination" de scan (elle apparait
   alors dans la liste du panneau).
2. Il surveille en continu si cette destination a ete choisie et le bouton "Scan" presse.
3. Quand c'est le cas, il envoie la sequence SNMP qui declenche reellement le moteur de scan
   (le simple polling HTTP ne suffit pas — c'est la piece manquante qui a pris le plus de
   temps a trouver).
4. Il recupere l'image scannee (JPEG) sur un port TCP dedie, et la sauvegarde en PDF.

## Prerequis

- Python 3 + [Pillow](https://pypi.org/project/Pillow/) (`pip install pillow` ou paquet
  systeme `python3-pil`)
- `snmpset`/`snmpget` (paquet `snmp` sur Debian/Ubuntu, `net-snmp` ailleurs)
- Etre sur le meme reseau local que l'imprimante (protocole non authentifie, prevu pour du
  LAN de confiance)

## Configuration

Tout se regle via variables d'environnement :

| Variable | Defaut | Description |
|---|---|---|
| `PRINTER_IP` | `192.168.1.2` | IP de l'imprimante |
| `HOST_ID` | `MYPC` | Doit correspondre au **nom NetBIOS reel** de la machine qui lance le script (l'imprimante semble le resoudre par NBNS avant d'accepter le scan) |
| `DEST_DISPLAY` | `MYPC:AutoScan` | Nom affiche au panneau de l'imprimante dans la liste des destinations |
| `OUTPUT_DIR` | `./output` | Dossier ou sont deposes les PDF generes |
| `SNMP_COMMUNITY` | `internal` | Communaute SNMP (valeur observee sur le modele teste) |

```bash
export PRINTER_IP=192.168.1.50
export HOST_ID=$(hostname | tr a-z A-Z)
export DEST_DISPLAY="$(hostname):Scan"
export OUTPUT_DIR=/srv/scans
python3 hp3055_scan_bridge.py
```

Le script tourne en boucle indefiniment (`Ctrl+C` pour arreter), et journalise chaque etape
sur stdout. Pour un fonctionnement permanent, en faire un service systemd (`Restart=always`).

## Limitations connues

- Resolution figee a 150 dpi couleur (valeurs SNMP observees, non parametrees).
- Non teste avec le bac d'alimentation automatique (ADF) multi-pages.
- Testé sur un HP LaserJet 3055 ; les autres modeles de la meme gamme partagent
  vraisemblablement le meme firmware/moteur, mais ce n'est pas confirme.
- Un `DestinationID` de slot 0 a parfois semble en echec pendant le developpement ; le script
  sacrifie ce slot par precaution, mais la cause reelle n'a pas ete confirmee avec certitude
  (voir `PROTOCOL.md`).

## Licence

Domaine public / usage libre — protocole issu d'une retro-ingenierie a but d'interoperabilite
sur du materiel personnel. Aucune garantie.
