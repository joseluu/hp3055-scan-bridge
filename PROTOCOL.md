# Protocole "Scan to PC" du HP LaserJet 3050/3052/3055/3390/3392 — reverse engineering

Capture de reference obtenue en enregistrant une destination de scan depuis le logiciel HP
"Full Solution" (tournant dans une VM Windows 2000, l'installeur d'epoque ne s'installant
pas proprement sur un OS moderne), puis en selectionnant cette destination au panneau du
HP3055 et en lancant un scan, tout en capturant le trafic reseau (port miroir du switch).

Le HP3055 embarque un serveur web (`Virata-EmWeb/R6_0_1`) exposant une API HTTP/XML
proprietaire HP ("PML"/imaging schema `http://www.hp.com/schemas/imaging/pls/dev/1.0`) sur le
port 80. Aucune authentification n'est requise sur le reseau local.

## 1. Enregistrement d'une destination (une fois, depuis la config du logiciel HP)

Requete observee (User-Agent distinctif `ScanDestSetup`) :

```
POST /hp/device/set_config.html HTTP/1.1
User-Agent: ScanDestSetup
Host: 192.168.1.2
Content-Length: 241
Connection: Keep-Alive
Cache-Control: no-cache

AddScanToDest_1=MYPC^MYPC:Email Doc^DestFolder
&AddScanToDest_2=MYPC^MYPC:Email Pic^DestFolder
&AddScanToDest_3=MYPC^MYPC:File^DestFolder
&AddScanToDest_4=MYPC^MYPC:SCAN^DestFolder
```
(`^` = `%5e`, `:` = `%3a` url-encodes ; corps reel sur une seule ligne, mis en forme ici pour
lisibilite). Format de chaque entree `AddScanToDest_N` : `<HostID>^<DeviceDisplay>^<ScanToType>`.
- `HostID` : nom de la machine hote (le nom NetBIOS reel de la machine — voir section 3bis,
  le firmware semble le resoudre par NBNS).
- `DeviceDisplay` : libelle affiche au panneau du 3055 dans la liste des destinations.
- `ScanToType` : toujours `DestFolder` observe ici (autres valeurs possibles non testees, ex
  `DestEmail` ?).

Reponse : `200 OK`, corps XML `<Resets><ResetType>0</ResetType></Resets>` (chunked).

On peut relire la liste des destinations enregistrees avec :
```
GET /hp/device/info_scanto_destinations.xml HTTP/1.1
```
=> XML `<ScanToDestinations>` listant chaque `<ScanToDestination DestinationID="N">` avec
`<HostID>`, `<DeviceDisplay>`, `<ScanToType>`. Les destinations sont **volatiles** : un
redemarrage du 3055 les efface toutes (le vrai logiciel HP les reenregistre a chaque
demarrage).

## 2. Boucle d'attente ("le logiciel HP guette le bouton du panneau")

Le proxy/service HP local (vu comme client HTTP avec `User-Agent: hp Proxy/3.0`,
`Host: localhost:3910` — indique un service local sur le PC qui ecoute en `:3910` et relaie
vers le 3055) **poll toutes les ~6 secondes** :

```
GET /hp/device/notifications.xml HTTP/1.1
CONTENT-LENGTH:0
HOST:localhost:3910
USER-AGENT:hp Proxy/3.0
```

Reponse XML `<Notifications>` avec plusieurs blocs. Le bloc pertinent :

```xml
<ScanToNotifications>
  <ScanToDeviceDisplay></ScanToDeviceDisplay>
  <ScanToHostID></ScanToHostID>
  <ScanToNotSetup>0</ScanToNotSetup>
  <ADFLoaded>0</ADFLoaded>
</ScanToNotifications>
```

**A l'etat de repos, `ScanToDeviceDisplay` et `ScanToHostID` sont vides.** Des que l'utilisateur
choisit une destination au panneau et lance le scan, LA MEME requete de polling recoit une
reponse ou ces deux champs sont remplis avec les valeurs exactement enregistrees a l'etape 1
(ex: `<ScanToDeviceDisplay>MYPC:Email Doc</ScanToDeviceDisplay>` /
`<ScanToHostID>MYPC</ScanToHostID>`). C'est le signal "destination choisie" — mais **ce n'est
pas ce qui declenche physiquement le scan**, voir section 3bis.

## 3. Recuperation de l'image scannee — port TCP 8290 (protocole brut, pas HTTP)

Une fois le scan physiquement demarre (section 3bis), le client ouvre une connexion TCP brute
vers `<IP imprimante>:8290` (pas de HTTP du tout). Le serveur envoie directement :

- **Un en-tete binaire proprietaire (~74 octets observes)** : structure exacte non entierement
  decodee, mais on y trouve les valeurs de resolution (XRes/YRes en DPI) et les dimensions en
  pixels de l'image, en clair, peu avant le debut du JPEG.
- **Immediatement suivi du JPEG complet** : marqueur SOI+APP0 `FFD8 FFE0 ... 4A46 4946 00`
  (JFIF), jusqu'au marqueur EOI `FFD9`.

Le delai entre la connexion TCP et le premier octet recu correspond au temps physique du scan
(observe entre ~18s et ~20s). Pas d'autre echange sur ce port : le serveur pousse les donnees
des l'etablissement de la connexion, le client se contente d'ACKer et de fermer une fois la
lecture terminee.

## 3bis. Le vrai declencheur du scan physique : SNMP, pas HTTP

Le point 2 (`notifications.xml`) ne fait que **refleter** l'etat pour l'affichage panneau —
ce n'est PAS ce qui demarre mecaniquement le scan. Le vrai logiciel HP envoie en parallele
une sequence **SNMP v1** (communaute `"internal"`) vers des OID HP privees, capturee en
observant le trafic reseau du logiciel HP pendant un scan reussi (a capturer sur le trafic
du PC client vers l'imprimante, pas uniquement sur l'imprimante elle-meme si le PC qui
capture n'est pas celui qui scanne — un port miroir/SPAN configure sur le bon port de switch
est necessaire pour voir ce trafic depuis une troisieme machine).

Sequence observee (`SetRequest`, OID sous prefixe `.1.3.6.1.4.1.11.2.3.9.4.2.1.2.2.1`) :

| OID (suffixe) | Type       | Valeur observee           | Role probable |
|---|---|---|---|
| `.3.0`  | INTEGER    | **8 = niveaux de gris, 24 = couleur** (confirme sur 6 essais croises, voir tableau ci-dessous) | **mode du capteur (le seul vrai selecteur)** |
| `.2.0`  | Hex-STRING | `XX XX 00 00` repete 2x (XRes puis YRes, little-endian 16 bits utiles) | **resolution en DPI** : `C8 00...`x2 = 200dpi, `2C 01...`x2 = 300dpi, `96 00...`x2 = 150dpi |
| `.16.0` | INTEGER    | 0                         | ? |
| `.17.0` | INTEGER    | largeur en pixels, varie avec `.2.0` (2478-2480 a 200/300dpi pour du A4) | largeur en pixels, deduite de la resolution (~ DPI x 8.27") |
| `.50.0` | INTEGER    | 8409 (gris) / 8448 (couleur 300dpi) / 8409 (couleur 200dpi) | taille/buffer attendu, varie avec mode+resolution |
| `.76.0` | INTEGER    | 0                         | ? |
| `.4.0`  | INTEGER    | **toujours 6** dans tous nos essais (gris, couleur, 150/200/300dpi) | **PAS le selecteur de mode malgre l'hypothese initiale** — role encore inconnu |
| `.53.0` | Hex-STRING | `33 33 02 00`             | ? |
| `.54.0` | INTEGER    | 1                         | ? (flag start-related) |
| `.12.0` | INTEGER    | **1→2 = GO**, passe a 5 une fois l'image prete, a remettre a 1 par le client apres recuperation | **etat de la machine a etats du scan** |

### Mode scan (.3.0) et resolution (.2.0/.17.0) — confirme par tests croises (2026-10-03)

Six scans tests effectues depuis le vrai logiciel HP (VM), en faisant varier mode et
resolution independamment, destination et format de sortie differents a chaque fois,
capture reseau a chaque essai :

| Test | Format sortie (PC) | `.3.0` | `.2.0`/`.17.0` (DPI deduit) | Image obtenue (verifiee) |
|---|---|---|---|---|
| JOSE:File | — | 24 | 150dpi, 2544px | couleur |
| JOSE:TEST_JPG | JPG | 24 | 300dpi, 2528px | couleur (confirme par logiciel : "millions of colors" 200dpi affiche, mais .2.0 montre 300 — le logiciel demande une resolution native puis sous-echantillonne lui-meme en local) |
| JOSE:TEST_1B (wizard A4/BW/300dpi) | TIFF | 8 | 300dpi, 2480px | **A4 BW confirme** |
| JOSE:TEST_CO_J (couleur 200dpi A4) | JPG | 24 | 200dpi, 2478px | couleur |
| JOSE:TEST_CO_P (memes parametres) | PDF | 24 | 200dpi, 2478px | couleur — **SNMP et flux port 8290 identiques a l'octet pres au test JPG precedent** |
| JOSE:TEST_GR_P / TEST_GR_J (grayscale 300dpi) | PDF puis JPG | 8 | 300dpi, 2480px | **niveaux de gris confirme** (pas du noir/blanc 1-bit pur) |

**Conclusions etablies :**
- `.3.0` est le seul vrai selecteur de mode cote imprimante, et il n'a que **2 valeurs
  possibles** : `8` et `24`. Pas de 3e valeur pour "noir et blanc" distinct de "niveaux de
  gris" — les deux rendus (`TEST_1B` et `TEST_GR_*`) donnent exactement la meme sequence
  SNMP (`.3.0=8`, memes autres champs). **La distinction grayscale vs BW/1-bit est donc un
  post-traitement logiciel (seuillage) applique par le PC apres reception du JPEG en
  niveaux de gris**, pas un reglage materiel de l'imprimante.
- `.2.0` encode directement le DPI demande (XRes/YRes identiques, codes sur les 2 premiers
  octets de chaque mot de 4 octets, le reste a 0), et `.17.0` est la largeur en pixels
  coherente pour une page A4 a cette resolution. Les deux valeurs bougent ensemble et sont
  independantes du mode (`.3.0`).
- **Le format de sortie choisi dans le logiciel HP (JPG/PDF/TIFF/...) n'a aucune influence
  sur le trafic reseau** : capture rigoureusement identique (SNMP et donnees port 8290)
  entre un essai "JPG" et un essai "PDF" avec les memes reglages mode/resolution/destination.
  L'imprimante envoie **toujours** du JPEG brut sur le port 8290 ; la conversion vers
  PDF/TIFF/etc. est faite localement par le logiciel PC apres reception.
- **Le niveau de compression JPEG (qualite/taille de fichier) n'est pas non plus pilote par
  SNMP.** Deux scans consecutifs, memes reglages (gris, 300dpi, meme destination), seule la
  compression changee dans le logiciel HP ("default" vs "smallest file size") : sequences
  SNMP **rigoureusement identiques a l'octet pres** (seul le nonce aleatoire `.1.1.1.25.0`
  differe, sans rapport). Pourtant le volume reellement transfere sur le port 8290 differe
  nettement : ~99 Ko (default) vs ~83 Ko (smallest file size), soit ~16% de moins. La
  compression est donc **decidee par l'imprimante elle-meme** (elle sait produire un JPEG
  plus ou moins compresse) mais **pas via un des OID connus** — soit via un champ encore
  marque "?" dans le tableau ci-dessus (candidats : `.53.0` ou `.76.0`, jamais vus varier
  dans nos essais mais pas testes isolement pour la compression), soit via l'en-tete binaire
  de 74 octets avant le JPEG sur le port 8290 (non decode), soit via un mecanisme totalement
  different qu'on n'a pas encore identifie. **Non elucide — a creuser.**

Il y a aussi un champ `.1.3.6.1.4.1.11.2.3.9.4.2.1.1.1.25.0` (Hex-STRING 16 octets) qui
alterne entre zero et des valeurs a priori aleatoires plusieurs fois avant le `GO` — role non
elucide (heartbeat ? nonce ?). **Pas necessaire** : le scan fonctionne sans le reproduire.

**Sequence minimale qui fonctionne** (voir `hp3055_scan_bridge.py`) : poser les 9 valeurs de
configuration ci-dessus, puis `SET .12.0 = 2` pour demarrer physiquement le scan. Le moteur
demarre alors avec un delai mecanique de ~15-20s avant que les donnees soient disponibles sur
le port 8290 (section 3). Une fois l'image recuperee, remettre `.12.0 = 1` (sinon l'OID reste
bloque et un nouveau `SET .12.0 = 2` est refuse avec `noSuchName` — la transition n'est
acceptee que depuis l'etat 1).

Outils pour experimenter manuellement : `snmpget`/`snmpset` (paquet Debian/Ubuntu `snmp`,
net-snmp), ex. :
```
snmpset -v1 -c internal <IP imprimante> .1.3.6.1.4.1.11.2.3.9.4.2.1.2.2.1.12.0 i 2
```

Point important : cette sequence SNMP ne semble **pas liee a une destination particuliere**
(rien dans les paquets SNMP n'identifie HostID/DeviceDisplay) — elle pilote juste le moteur de
scan lui-meme. Le routage vers la bonne destination HTTP (`notifications.xml`) et le port 8290
comme canal de recuperation restent lies au mecanisme de destination des sections 1-2.

## Ce qu'il reste a clarifier

- Decodage exact de l'en-tete binaire avant le JPEG (offsets precis de largeur/hauteur/dpi/
  autres flags).
- Role exact de chaque OID SNMP encore marque "?" ci-dessus, et du champ `.25.0` a 16 octets.
- Comportement si plusieurs pages (bac ADF) : une connexion par page ? meme connexion pour
  plusieurs images concatenees ? Non teste.
- Role exact du champ `<StartScan>` (bloc `<StartScanNotifications>` de `notifications.xml`,
  jamais vu a `1` dans nos captures) — peut-etre un mode "scan generique" sans destination
  specifique.
- Confirmer si `ScanToType` accepte d'autres valeurs que `DestFolder`.

## Modeles concernes

Reverse-engineere sur un **HP LaserJet 3055** (firmware `Virata-EmWeb/R6_0_1`). Le
"Full Solution" installer d'epoque couvre aussi les 3050/3052/3390/3392 qui partagent
vraisemblablement le meme moteur/API — non teste sur ces modeles, retours bienvenus.
