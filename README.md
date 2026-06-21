# UNAS stock feed sync MVP

Ez a projekt egy Python alapú MVP UNAS készletfeed-szinkronhoz.
A script online CSV feedet tölt le, validálja a cikkszám és készlet mezőket, riportot készít, és opcionálisan limitált live módban UNAS `setStock` készletfrissítést küld.

Alapértelmezés szerint dry-run módban fut. Ilyenkor nincs UNAS API hívás és nincs éles módosítás.

## Mire való?

A CSV-ben szereplő SKU és készlet értékek alapján ellenőrző riport készül a `reports/` mappába.
Live módban csak a valid sorokból készül UNAS `setStock` XML, ahol az `<Action>modify</Action>` az összmennyiséget állítja be.
Ha a CSV-ben `stock = 8`, akkor az UNAS készlet 8 lesz, nem 8 darabbal növekszik.

## Telepítés lokálisan

```bash
pip install -r requirements.txt
```

Hozz létre saját `.env` fájlt a `config.example.env` alapján:

```bash
cp config.example.env .env
```

Windows PowerShell alatt:

```powershell
Copy-Item config.example.env .env
```

## Környezeti változók

```env
CSV_URL=
CSV_SKU_COLUMN=sku
CSV_STOCK_COLUMN=stock
CSV_DELIMITER=,
CSV_ENCODING=utf-8
CSV_SKU_REMOVE_LEADING_ZERO=false
REPORT_DIR=reports
UNAS_API_KEY=
UNAS_API_BASE_URL=https://api.unas.eu/shop
```

Ha a CSV cikkszámai egy kezdő nullát tartalmaznak, de az UNAS cikkszámok nem,
kapcsold be ezt az opciót:

```env
CSV_SKU_REMOVE_LEADING_ZERO=true
```

Ez pontosan egy kezdő nullát távolít el. A riport `sku` mezője megtartja az
eredeti CSV értéket, az `unas_sku` mező pedig az UNAS-nak küldött értéket mutatja.

## Dry-run futtatás

Dry-run módban a script letölti és validálja a CSV-t, majd riportot készít. Nem loginol az UNAS API-ba és nem küld készletfrissítést.

```bash
python stock_feed_sync.py --dry-run
```

Egyedi CSV URL-lel:

```bash
python stock_feed_sync.py --csv-url "https://example.com/feed.csv"
```

Egyedi oszlopnevekkel:

```bash
python stock_feed_sync.py --csv-url "https://example.com/feed.csv" --sku-column "sku" --stock-column "stock"
```

Pontosvesszős CSV-hez:

```bash
python stock_feed_sync.py --csv-url "https://example.com/feed.csv" --delimiter ";"
```

## Limitált live teszt

Live módban a script ténylegesen módosítja az UNAS készleteket. Első éles tesztre ezt használd:

```bash
python stock_feed_sync.py --live --limit 5
```

További limitált futás:

```bash
python stock_feed_sync.py --live --limit 10
```

Ha `--live` van, de nincs `--limit`, a script lefut, de erős figyelmeztetést ír ki, mert minden valid sort elküld.

## UNAS API kulcs

Az UNAS admin felületen hozz létre API kulcsot az API beállításoknál.
A kulcsnak legalább ezekhez a műveletekhez kell jogosultság:

- login
- setStock

Az API kulcsot ne írd kódba és ne commitold. Lokálisan `.env` fájlban add meg:

```env
UNAS_API_KEY=sajat_api_kulcs
```

## GitHub Secrets

A GitHub repositoryban állítsd be ezeket:

- `CSV_URL`: az online CSV feed URL-je
- `UNAS_API_KEY`: az UNAS API kulcs

Beállítás menete:

1. Nyisd meg a GitHub repositoryt.
2. Menj a `Settings` menübe.
3. Válaszd a `Secrets and variables` > `Actions` oldalt.
4. Kattints a `New repository secret` gombra.
5. Add hozzá a `CSV_URL` és `UNAS_API_KEY` értékeket.

## GitHub Actions

A workflow neve: `Stock feed dry-run`.

Indítás:

1. Nyisd meg a repository `Actions` fülét.
2. Válaszd ki a `Stock feed dry-run` workflow-t.
3. Kattints a `Run workflow` gombra.
4. Válassz módot:
   - `dry-run`: csak validálás és riport
   - `live`: UNAS készletfrissítés
5. Live módnál hagyd meg vagy állítsd be a `limit` értéket.

A futás után a riport a workflow artifactjai között letölthető.

## Validációs szabályok

Hibás sor lesz, ha:

- a cikkszám üres,
- a készletmező üres,
- a készlet nem alakítható számmá,
- a készlet negatív,
- a készlet tizedes érték,
- a cikkszám duplikált.

A `0` készlet érvényes érték. A cikkszám és készlet mezők elejéről és végéről a szóközöket levágja a script.

## Riport mezői

```csv
row_number,sku,unas_sku,input_stock,status,message,unas_status,unas_error
```

Dry-run példa:

```csv
row_number,sku,unas_sku,input_stock,status,message,unas_status,unas_error
2,ABC-001,ABC-001,5,valid,ready_for_update,,
```

Live példa:

```csv
row_number,sku,unas_sku,input_stock,status,message,unas_status,unas_error
2,ABC-001,ABC-001,5,updated,stock_updated,ok,
3,ABC-999,ABC-999,3,error,unas_error,error,Product not found
```

## Biztonsági megjegyzés

Az alapértelmezett futás dry-run. Éles UNAS készletmódosítás csak `--live` kapcsolóval történik.
Az API kulcs és a token nem kerül konzol logba.
