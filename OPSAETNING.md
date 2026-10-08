# Opsætning: Cloudflare R2 + Flask-server

Sådan hænger det sammen:

```
Besøgende ──► Cloudflare (DNS, CDN, beskyttelse)
                 ├─ billeder.ditdomæne.dk ──► R2-bucket (originaler + thumbnails, ca. 113 GB)
                 └─ ditdomæne.dk ──► Flask-server (sider, søgning, login, upload)
                                        └─ lille disk med gallery.db (SQLite)
```

Serveren sender **ingen billeder**. Browseren henter dem direkte fra R2 via
Cloudflares CDN. Derfor koster trafikken ikke noget, og serveren kan være lille.

> Priser og menuer hos Cloudflare/Render ændrer sig. Tjek altid den aktuelle
> prisside, og læs menunavnene som vejledende. R2 kostede da dette blev skrevet
> 0,015 $ pr. GB pr. måned for lagring, 10 GB gratis, og **0 $ for udgående trafik**.
> Til ca. 113 GB er det omkring 1,55 $ om måneden.

Gør tingene i denne rækkefølge.

---

## 1. Domænet skal ligge hos Cloudflare

1. Opret en gratis konto på cloudflare.com.
2. **Add a site** og skriv dit domæne. Vælg den gratis plan.
3. Cloudflare giver to *nameservers*. Skriv dem ind hos der, hvor du købte domænet.
4. Vent til Cloudflare viser at domænet er **Active**.

(Det er et krav for at R2 kan bruge dit eget domæne til billederne.)

## 2. Opret R2-bucket og offentligt billeddomæne

1. I Cloudflare: **R2 Object Storage** → **Create bucket**. Navn fx `bus-gallery`.
   Du skal angive et betalingskort for at bruge R2, også selvom du holder dig i gratis-kvoten.
2. Åbn bucketen → **Settings** → **Custom Domains** → **Connect Domain**.
   Skriv fx `billeder.ditdomæne.dk`. Cloudflare opretter selv DNS-posten.
   Brug **ikke** den gratis `r2.dev`-adresse til den rigtige side. Den er til test og er begrænset.
3. Skriv `https://billeder.ditdomæne.dk` ned. Det er din `R2_PUBLIC_URL`.

### API-nøgle til appen

1. **R2 Object Storage** → **Manage API tokens** → **Create API token**.
2. Rettighed: **Object Read & Write**. Begræns den til din bucket.
3. Gem **Access Key ID** og **Secret Access Key** (vises kun én gang).
4. Dit **Account ID** står på R2-oversigtssiden (højre side).

## 3. Flyt de eksisterende billeder til R2

Det gøres **ikke** via admin-siden, men med værktøjet *rclone* fra din computer.
113 GB kan tage timer. Brug en stabil forbindelse, og lad computeren være tændt.
`rclone copy` kan genoptages, hvis det afbrydes. Kør bare kommandoen igen.

1. Installer rclone (rclone.org/downloads).
2. Kør `rclone config` → **n** (new remote) → navn `r2` → type **s3** →
   provider **Cloudflare** → indsæt Access Key ID og Secret → endpoint
   `https://<DIT-ACCOUNT-ID>.r2.cloudflarestorage.com` → resten Enter.
   Hvis din nøgle kun gælder én bucket, så tilføj `no_check_bucket = true`
   under `[r2]` i rclone-konfigurationsfilen.
3. Kopiér thumbnails først (små, så du kan teste hurtigt), derefter originalerne.
   Peg på mappen `data` fra det gamle projekt:

```bash
rclone copy ./data/thumbnails r2:bus-gallery/thumbnails --transfers 16 --checkers 16 --progress \
  --header-upload "Cache-Control: public, max-age=31536000, immutable"

rclone copy ./data/uploads r2:bus-gallery/uploads --transfers 16 --checkers 16 --progress \
  --header-upload "Cache-Control: public, max-age=31536000, immutable"
```

4. Tjek at alt kom med. Værktøjet sammenligner databasen med bucketen:

```bash
pip install -r requirements.txt
export R2_BUCKET=bus-gallery R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
       R2_PUBLIC_URL=https://billeder.ditdomæne.dk
python tools/check_r2.py --db data/gallery.db
```

Mangler der filer, så kør `python tools/check_r2.py --db data/gallery.db --upload-missing data`.

5. Åbn i en browser `https://billeder.ditdomæne.dk/thumbnails/<et-filnavn>.jpg`. Billedet skal vises.

> **Originalerne røres ikke.** De kopieres bit for bit. Thumbnails er den eksisterende
> JPEG-version, som allerede er lavet af appen.

## 4. Sæt serveren op (eksempel: Render)

Serveren skal kun have en lille disk (til databasen), så 1-2 GB er rigeligt.

1. Læg koden på GitHub. `.gitignore` sørger for at `venv/` og `data/` ikke kommer med.
2. Render → **New** → **Web Service** → vælg dit repository.
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 2 --threads 4 --timeout 120`
   - **Instance type:** en betalt type (gratis typer har ikke disk og går i dvale).
3. **Disks** → Add Disk, mount path `/var/data`, 1 GB.
4. **Environment** (se `.env.example`):

| Navn | Værdi |
|---|---|
| `PYTHON_VERSION` | `3.12.3` (eller en anden 3.12-version Render tilbyder) |
| `DATA_DIR` | `/var/data` |
| `SECRET_KEY` | lang tilfældig tekst: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD_HASH` | output fra `python tools/make_password_hash.py` (din fars adgangskode) |
| `R2_BUCKET` | `bus-gallery` |
| `R2_ACCOUNT_ID` | dit Account ID |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | API-nøglen fra trin 2 |
| `R2_PUBLIC_URL` | `https://billeder.ditdomæne.dk` |
| `TRUST_CLOUDFLARE` | `1` |
| `CONTACT_EMAIL` | (valgfri) |

Giv din far en **lang adgangskode** (mindst 12 tegn, fx 4-5 tilfældige ord).
Appen starter ikke, hvis `SECRET_KEY` eller `ADMIN_PASSWORD_HASH` mangler.

5. **Health check path** på Render: `/healthz`.

### Flyt den eksisterende database

Databasen (`gallery.db`) er lille. Læg den i R2 og hent den ned på serveren:

```bash
# på din computer
rclone copyto ./data/gallery.db r2:bus-gallery/backups/gallery-import.db
```

I Render's **Shell** på servicen:

```bash
python tools/restore_db.py backups/gallery-import.db --force
```

Genstart derefter servicen (**Manual Deploy → Restart**). Appen opdaterer selv databasens
opbygning ved start (nye indekser og tabeller). Eksisterende data bevares.

## 5. Sæt hjemmesiden på Cloudflare

1. Render → **Settings → Custom Domains** → tilføj `ditdomæne.dk` (eller `www`).
   Render viser den adresse, der skal pege på.
2. Cloudflare → **DNS** → opret en `CNAME` (`@` eller `www`) til Renders adresse, **Proxied** (orange sky).
3. **SSL/TLS** → vælg **Full (strict)**.
4. **SSL/TLS → Edge Certificates** → slå **Always Use HTTPS** til. Overvej at slå **HSTS** til, når alt virker.
5. **Security → Bots** → slå **Bot Fight Mode** til.

### Lås serveren, så al trafik går gennem Cloudflare (anbefalet)

Uden dette kan nogen kalde Renders egen adresse direkte og gå uden om Cloudflare.

1. Vælg en lang tilfældig tekst, fx fra `secrets.token_hex(32)`.
2. Cloudflare → **Rules → Transform Rules → Modify Request Header** → opret en regel for hele sitet:
   **Set static** header `X-Origin-Secret` = din tekst.
3. På Render: sæt `ORIGIN_SECRET` til samme tekst.

Herefter svarer serveren `403` til alle, der ikke kommer via Cloudflare (kun `/healthz` er undtaget).

### Rate limiting hos Cloudflare (ekstra lag)

Appen låser selv login pr. IP-adresse efter 5 forkerte forsøg i 15 minutter. Det er det
vigtigste værn mod brute force. Den gratis Cloudflare-plan har kun **én**
rate-limiting-regel med 10 sekunders vindue. Brug den til login:

**Security → WAF → Rate limiting rules → Create rule**: *URI Path equals `/login`* og
*Method equals POST*, 5 forespørgsler pr. 10 sekunder → Block.

## 6. Test før du giver adgangen videre

- [ ] Forsiden viser kategorier med billeder (der kommer fra `billeder.ditdomæne.dk`).
- [ ] Et billede åbnes, og **Download photo** gemmer originalen.
- [ ] Forkert adgangskode 5 gange låser login ("Too many wrong attempts"). Vent 15 minutter, eller ryd tabellen `rate_events`.
- [ ] Login med den rigtige adgangskode → upload 2-3 testbilleder → de dukker op i R2 (`uploads/` og `thumbnails/`).
- [ ] Slet testbillederne → de forsvinder også fra R2.
- [ ] `https://dit-site.onrender.com` (den direkte adresse) giver `403`, hvis du har sat `ORIGIN_SECRET`.

## 7. Backup (vigtigt)

**R2 er ikke en backup.** Slettes et billede i appen, er det væk. Du skal have en kopi et andet sted.

- **Originalerne:** behold den oprindelige mappe på en computer eller ekstern disk, og kopiér nye
  billeder løbende, fx `rclone copy r2:bus-gallery/uploads ./backup/uploads` en gang om måneden.
- **Databasen:** når appen kører med R2, tager den **selv en backup en gang i døgnet** til
  `backups/` i bucketen og beholder de nyeste 14 (slå fra med `AUTO_BACKUP=0`). Du kan også tage
  en ekstra kopi med det samme, fx før større ændringer, fra webservicens Shell:
  `python tools/backup_db.py`. Databasen indeholder alt, din far har skrevet (titler, tags, album),
  så den er vigtig at have.
- **Gendan:** `python tools/restore_db.py --latest --force`, og genstart derefter servicen.

## Kendte begrænsninger

- Detaljesiden viser den **originale** fil (op til ca. 5 MB), ikke en mellemstørrelse. Det er hurtigt nok
  via CDN'et, men en mellemstørrelse kunne spare data på mobil. Kan tilføjes senere.
- Originalerne beholder deres EXIF-data (inkl. evt. GPS), fordi de skal kunne hentes uændret.
  Thumbnails har ingen EXIF-data.
- Rate limiting og lås bygger på besøgendes IP-adresse. Uden `TRUST_CLOUDFLARE=1` bag Cloudflare
  ser serveren kun Cloudflares adresser.
