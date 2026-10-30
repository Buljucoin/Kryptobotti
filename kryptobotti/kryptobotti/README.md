# Kryptobotti

Paperikauppabotti, joka seuraa bitcoinin (BTC) ja etherin (ETH) trendiä päiväkynttilöillä ja tekee osto- ja myyntipäätökset kerran vuorokaudessa. Mitään oikeaa rahaa ei liiku. Botti pyörii ilmaiseksi GitHubin palvelimilla, eikä tietokoneesi tarvitse olla päällä.

## Miten botti päättää

Joka yö, kun edellinen päivä on sulkeutunut (klo 00.00 UTC), botti tarkistaa kummallekin kryptolle kaksi ehtoa:

1. **Lyhyt trendi:** 10 päivän EMA on 30 päivän EMA:n yläpuolella.
2. **Pitkä trendi:** päätöskurssi on 150 päivän keskiarvon yläpuolella.

- **Osto:** molemmat ehdot täyttyvät eikä positiota ole.
- **Myynti:** kumpi tahansa ehto lakkaa täyttymästä.

Salkku alkaa 10 000 eurosta, joka jaetaan BTC:lle ja ETH:lle puoliksi. Kumpikin puolisko toimii itsenäisesti. Kurssit tulevat Krakenin julkisesta rajapinnasta euroina. Kaupankäyntikuluksi oletetaan 0,4 % per kauppa.

Historiatesteissä (BTC/USDT 2019–2026) sääntö tuotti +3 208 % (osta ja pidä +2 097 %), ja pahin lasku oli −40 % (osta ja pidä −77 %). Menneet tulokset eivät takaa tulevia.

## Käyttöönotto (noin 15 minuuttia)

### 1. Luo GitHub-tili
Jos sinulla ei ole tiliä, luo se osoitteessa https://github.com/signup. Ilmainen tili riittää.

### 2. Luo repositorio
1. Kirjauduttuasi paina oikeasta yläkulmasta **+** ja valitse **New repository**.
2. Nimeksi esim. `kryptobotti`.
3. Valitse **Public**. Julkisissa repositorioissa ajastetut ajot ovat ilmaisia. Kaikki näkevät botin koodin ja paperikaupat, mutta niissä ei ole henkilötietojasi.
4. Paina **Create repository**.

### 3. Lataa tiedostot
1. Pura saamasi zip-paketti koneellesi.
2. Paina repositorion sivulla **uploading an existing file** (tai **Add file → Upload files**).
3. Raahaa kaikki puretun kansion tiedostot ikkunaan: `bot.py`, `config.json`, `README.md`, `.gitignore` ja kansio `.github`.
4. Paina **Commit changes**.

**Tarkista, että `.github`-kansio tuli mukaan.** Piste nimen alussa tekee siitä piilotetun kansion, jota Mac ja Windows eivät aina näytä. Jos repositoriossa ei näy kansiota `.github`, tee näin:
1. **Add file → Create new file**
2. Kirjoita nimeksi täsmälleen `.github/workflows/botti.yml` (vinoviivat luovat kansiot).
3. Kopioi sisällöksi alla oleva teksti kokonaan ja paina **Commit changes**.

<details><summary>botti.yml</summary>

```yaml
name: Kryptobotti

on:
  schedule:
    # joka päivä klo 00.17 UTC (02.17 / 03.17 Suomen aikaa), kun Krakenin päiväkynttilä on sulkeutunut
    - cron: "17 0 * * *"
  workflow_dispatch: {}   # mahdollistaa ajon käsin Actions-välilehdeltä

permissions:
  contents: write   # tilan ja raportin tallennus repoon
  issues: write     # ilmoitus kaupoista

concurrency:
  group: kryptobotti
  cancel-in-progress: false

jobs:
  aja:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Päivän päätös
        run: python bot.py

      - name: Tallenna tila ja raportti
        run: |
          git config user.name "kryptobotti"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          for f in state.json RAPORTTI.md paivakirja.csv kaupat.csv; do
            [ -f "$f" ] && git add "$f"
          done
          if git diff --cached --quiet; then
            echo "Ei muutoksia tallennettavaksi"
          else
            git commit -m "Botin päivitys $(date -u +%Y-%m-%d)"
            git push
          fi

      - name: Ilmoita kaupoista
        if: hashFiles('ilmoitus.md') != ''
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh issue create \
            --title "Kryptobotti: paperikauppa $(date -u +%d.%m.%Y)" \
            --body-file ilmoitus.md \
            --assignee "${{ github.repository_owner }}"
```

</details>

### 4. Salli botin tallentaa tulokset
1. Repositoriossa **Settings → Actions → General**.
2. Kohdassa **Workflow permissions** valitse **Read and write permissions**.
3. Paina **Save**.

### 5. Käynnistä ensimmäinen ajo
1. Avaa **Actions**-välilehti. Jos GitHub kysyy, salli työnkulut painamalla vihreää painiketta.
2. Valitse vasemmalta **Kryptobotti** ja paina **Run workflow → Run workflow**.
3. Noin minuutin päästä repositorioon ilmestyvät tiedostot `RAPORTTI.md`, `state.json` ja `paivakirja.csv`.

Tämän jälkeen botti ajaa itsensä joka yö klo 00.17 UTC (Suomen aikaa 02.17 talvella, 03.17 kesällä). GitHub voi joskus viivästää ajastettuja ajoja hieman.

## Tulosten seuraaminen

- **RAPORTTI.md:** salkun arvo, avoimet positiot, ostoehtojen tila ja viimeisimmät kaupat. Toimii myös puhelimen GitHub-sovelluksessa.
- **Sähköposti-ilmoitus:** aina kun botti tekee kaupan, se avaa repositorioon *issuen*, joka osoitetaan sinulle, ja GitHub lähettää siitä sähköpostin. Tarkista, että sähköposti-ilmoitukset ovat päällä kohdassa GitHubin **Settings → Notifications**.
- **kaupat.csv** ja **paivakirja.csv:** kaikki kaupat ja päivittäiset signaalit. Aukeavat Excelissä (erotin on puolipiste).
- Jos ajo epäonnistuu (esim. Kraken ei vastaa), GitHub lähettää siitä sähköpostin. Seuraavan yön ajo jatkaa normaalisti.

## Asetusten muuttaminen

Avaa `config.json` GitHubissa ja paina kynäkuvaketta:

| Asetus | Merkitys |
|---|---|
| `aloituspaaoma_eur` | paperisalkun koko |
| `kulu_prosenttia` | kaupankäyntikulu per kauppa; aseta pörssisi todellisen kulun mukaan |
| `nopea_ema`, `hidas_ema`, `trendiraja` | säännön jaksot päivinä (testatut arvot 10, 30, 150) |
| `osuus` | kunkin kryptan osuus salkusta |

Aloituspääoman tai osuuksien muutos tulee voimaan vasta, kun poistat tiedoston `state.json`. Silloin paperisalkku alkaa alusta.

## Komennot omalla koneella (valinnainen)

Tarvitset Python 3:n. Muita kirjastoja ei tarvita.

```
python bot.py --backtest   # säännön tulos Krakenin noin 2 vuoden historialla
python bot.py              # päivän päätös (sama kuin GitHub ajaa)
```

## Seuraava vaihe

Kun paperikauppa on pyörinyt muutaman kuukauden ja toimii odotetusti, sama botti voidaan kytkeä pörssin rajapintaan pienellä summalla. Silloin pörssin API-avain tallennetaan GitHubin salattuihin asetuksiin (**Settings → Secrets**), eikä se näy koodissa. Anna avaimelle vain kaupankäyntioikeus, ei koskaan nosto-oikeutta.

_Tämä on tekninen työkalu, ei sijoitusneuvontaa._
