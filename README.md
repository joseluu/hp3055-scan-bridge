# hp3055-scan-bridge

Fait fonctionner le bouton **"Scan" du panneau de controle** d'un HP LaserJet
3050/3052/3055/3390/3392 avec [scanservjs](https://github.com/sbs20/scanservjs), **sans** le
logiciel HP "Full Solution" d'epoque ni la VM Windows 2000 qu'il faut habituellement pour le
faire tourner.

Ces imprimantes tout-en-un supportent le "scan to PC" declenche depuis leur panneau physique,
mais ce mecanisme n'a jamais ete documente publiquement par HP et n'a rien a voir avec un
scan reseau classique (SANE/WSD/AirScan) : c'est un protocole proprietaire, moitie HTTP/XML,
moitie SNMP, entierement reverse-engineere ici. Voir [`PROTOCOL.md`](PROTOCOL.md) pour le
detail complet (requetes exactes, OID SNMP, structure des trames).

## Comment ça marche

Ce pont ne fait que la partie que ni SANE ni scanservjs ne savent faire : detecter l'appui du
bouton panneau (protocole proprietaire HP). Pour l'acquisition du scan elle-meme, il delegue
a scanservjs via son API HTTP locale, plutot que de reproduire le protocole SNMP de bout en
bout (voir "Pourquoi deleguer a scanservjs ?" ci-dessous pour la raison).

1. Le script s'enregistre aupres de l'imprimante comme plusieurs "destinations" de scan —
   une par profil couleur/resolution (elles apparaissent toutes dans la liste du panneau,
   l'imprimante n'ayant pas de bouton pour choisir le mode, c'est le choix de destination qui
   en tient lieu).
2. Il surveille en continu (polling HTTP) si l'une de ces destinations a ete choisie et le
   bouton "Scan" presse.
3. Quand c'est le cas, il appelle l'API `POST /api/v1/scan` de scanservjs avec le mode et la
   resolution du profil correspondant. scanservjs se charge de tout le reste (declenchement
   du scan via HPLIP/SANE, recuperation de l'image, conversion, sauvegarde) exactement comme
   si l'utilisateur avait clique sur "Scan" dans l'interface web.

### Pourquoi deleguer a scanservjs plutot que de tout reproduire soi-meme ?

Une premiere version de ce pont reproduisait elle-meme toute la sequence SNMP de
declenchement et recuperait le JPEG brut directement sur le port TCP 8290 de l'imprimante
(toujours documentee dans `PROTOCOL.md`, au cas ou ce pont serait utile sans scanservjs). Elle
fonctionnait, mais produisait des scans corrompus sur des documents denses (texte serre,
photos detaillees) — tres probablement un defaut du firmware de l'imprimante specifique a ce
chemin "push" historique, puisque scanservjs (qui passe par HPLIP/SANE, un chemin "pull"
completement distinct pour le meme materiel) ne montre jamais ce probleme, meme sur les memes
documents. Autant s'appuyer sur un chemin d'acquisition deja fiable plutot que de continuer a
debugger un protocole proprietaire non documente.

C'est aussi l'architecture que les mainteneurs de projets voisins recommandent pour ce genre
de probleme : un demon externe et decouple qui detecte l'evenement specifique au materiel,
puis delegue l'acquisition a l'outil de scan generique via son API — voir la discussion
[scanservjs#281](https://github.com/sbs20/scanservjs/issues/281) et
[sane-airscan#261](https://github.com/alexpevzner/sane-airscan/issues/261) (meme probleme
pour du materiel plus recent, WSD/eSCL, meme conclusion independante).

## Prerequis

- Python 3 (aucune dependance tierce : seulement la bibliotheque standard)
- Une instance de [scanservjs](https://github.com/sbs20/scanservjs) deja configuree et
  fonctionnelle pour cette imprimante (le pont ne fait qu'appeler son API, il ne scanne
  jamais lui-meme)
- Etre sur le meme reseau local que l'imprimante (protocole non authentifie, prevu pour du
  LAN de confiance)

## Configuration

Variables d'environnement :

| Variable | Defaut | Description |
|---|---|---|
| `PRINTER_IP` | `192.168.1.2` | IP de l'imprimante |
| `HOST_ID` | `MYPC` | Doit correspondre au **nom NetBIOS reel** de la machine qui lance le script (l'imprimante semble le resoudre par NBNS avant d'accepter le scan) |
| `SCANSERVJS_URL` | `http://127.0.0.1:8080` | URL de base de l'API scanservjs (le pont tourne generalement sur la meme machine que scanservjs) |
| `SCANSERVJS_DEVICE_ID` | *(vide, requis)* | Identifiant du device tel qu'expose par scanservjs — voir ci-dessous |

Pour trouver `SCANSERVJS_DEVICE_ID` :
```bash
curl -s http://127.0.0.1:8080/api/v1/context | python3 -m json.tool | grep '"id"'
# ex: "id": "hpaio:/net/HP_LaserJet_3055?ip=192.168.11.131"
```

```bash
export PRINTER_IP=192.168.1.50
export HOST_ID=$(hostname | tr a-z A-Z)
export SCANSERVJS_URL=http://127.0.0.1:8080
export SCANSERVJS_DEVICE_ID='hpaio:/net/HP_LaserJet_3055?ip=192.168.1.50'
python3 hp3055_scan_bridge.py
```

### Profils de scan (couleur/resolution/mode)

Le script enregistre **plusieurs destinations**, une par profil defini dans la constante
`PROFILES` en tete du script — le panneau de l'imprimante n'ayant pas de selecteur de mode,
c'est le choix de la destination qui en tient lieu. Chaque destination apparait au panneau
sous le nom `<HOST_ID>:<suffix>` (le panneau du 3055 n'affiche que 9 caracteres visibles
apres le HostID, d'ou des suffixes courts).

Profils fournis par defaut, chacun passe tel quel a l'API scanservjs. Format fixe a A4
(210x297mm, voir constante `A4_MM`) pour les 6 — les autres formats restent accessibles via
l'interface web de scanservjs, qui propose le choix a chaque scan :

| Suffixe | `mode` (scanservjs) | Resolution | Pipeline / qualite |
|---|---|---|---|
| `COLOR200` | `Color` | 200 dpi | PDF (JPG, qualite 75) |
| `COLOR300` | `Color` | 300 dpi | PDF (JPG, qualite 92) |
| `GRAY200` | `Gray` | 200 dpi | PDF (JPG, qualite 75) |
| `GRAY300` | `Gray` | 300 dpi | PDF (JPG, qualite 92) |
| `BW300` | `Lineart` | 300 dpi | PDF (TIFF, compression LZW) |
| `PDF_OCR` | `Lineart` | 300 dpi | PDF (JPG qualite 92) + calque de texte OCR (Tesseract) |

`PDF_OCR` produit un PDF dont le rendu visuel reste une image (comme les autres profils),
mais avec un calque de texte invisible superpose, genere par Tesseract — texte
selectionnable/cherchable sans changer l'apparence de la page. La langue OCR est configuree
via la variable d'environnement `OCR_LANG` du conteneur scanservjs (pas du bridge), par
exemple `OCR_LANG=fra` pour du francais — voir la config scanservjs elle-meme, hors scope de
ce depot.

`mode` doit correspondre a une des valeurs exposees par le backend SANE du device (champ
`--mode` dans `/api/v1/context`) ; `pipeline` doit correspondre a une des valeurs de
`devices[].settings.pipeline.options` du meme endpoint (controle le format de sortie —
PDF/JPG/PNG/TIFF, qualite, OCR...). Pour ajouter/modifier un profil, editer la liste
`PROFILES` dans le script.

**Pourquoi `BW300` utilise un pipeline different (TIFF/LZW, pas JPEG)** : JPEG compresse mal
du contenu 1-bit (texte en noir et blanc pur) — testé sur un document reel : ~1 Mo en JPEG
contre ~192 Ko en TIFF/LZW pour un resultat visuellement identique. La vraie compression
bitonale (CCITT Group 4, ~140 Ko sur le meme test) serait encore meilleure mais necessiterait
un pipeline personnalise dans la configuration scanservjs (`config.local.js`), non fait ici
pour rester avec les pipelines standard.

**Destinations volatiles** : une fois enregistree, une destination reste visible au panneau
jusqu'au prochain redemarrage de l'imprimante — y compris une destination qu'un profil
supprime de `PROFILES` (le pont n'enregistre jamais de destination absente de la liste mais
ne desenregistre pas non plus celles qui y etaient avant ; aucun mecanisme de suppression HTTP
n'a ete trouve malgre plusieurs tentatives, voir `PROTOCOL.md`). Pour nettoyer une destination
obsolete du panneau, redemarrer l'imprimante.

Le script tourne en boucle indefiniment (`Ctrl+C` pour arreter), et journalise chaque etape
sur stdout. Pour un fonctionnement permanent, en faire un service systemd (`Restart=always`).

## Limitations connues

- Necessite scanservjs deja installe et fonctionnel pour cette imprimante — ce pont n'est pas
  un outil de scan autonome, juste le detecteur de bouton manquant.
- Non teste avec le bac d'alimentation automatique (ADF) multi-pages.
- Testé sur un HP LaserJet 3055 ; les autres modeles de la meme gamme partagent
  vraisemblablement le meme firmware/moteur, mais ce n'est pas confirme.
- Un `DestinationID` de slot 0 a parfois semble en echec pendant le developpement ; le script
  sacrifie ce slot par precaution, mais la cause reelle n'a pas ete confirmee avec certitude
  (voir `PROTOCOL.md`).

## Licence

Domaine public / usage libre — protocole issu d'une retro-ingenierie a but d'interoperabilite
sur du materiel personnel. Aucune garantie.
