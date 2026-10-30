#!/usr/bin/env python3
"""
Kryptobotti: trendistrategia BTC:lle ja ETH:lle, paperikauppa.

Sääntö (päiväkynttilät, päätös kun päivä on sulkeutunut):
  OSTA  kun EMA(10) > EMA(30) JA päätöskurssi > SMA(150) eikä positiota ole
  MYY   kun kumpi tahansa ehto lakkaa täyttymästä

Kurssit: Krakenin julkinen rajapinta (EUR), ei vaadi tunnuksia.
Tila tallennetaan tiedostoon state.json, kaupat kaupat.csv:hen,
päivittäinen loki paivakirja.csv:hen ja yhteenveto RAPORTTI.md:hen.

Käyttö:
  python bot.py              ajaa päivän päätöksen (GitHub Actions tekee tämän)
  python bot.py --backtest   näyttää säännön tuloksen Krakenin ~720 päivän historialla
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DAY = 86400


def path(name):
    return os.path.join(ROOT, name)


def load_json(name, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(name, data):
    with open(path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- data

def fetch_kraken_daily(pair, data_dir=None, now=None):
    """Palauttaa listan sulkeutuneita päiväkynttilöitä: [(aika_s, close), ...]."""
    if data_dir:  # testikäyttö: lue tiedostosta
        with open(os.path.join(data_dir, pair + ".json"), encoding="utf-8") as f:
            payload = json.load(f)
    else:
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
        last_err = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "kryptobotti/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    payload = json.load(r)
                if payload.get("error"):
                    raise RuntimeError(", ".join(payload["error"]))
                break
            except Exception as e:  # verkko- tai rajapintavirhe: yritä uudelleen
                last_err = e
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"Kurssien haku epäonnistui ({pair}): {last_err}")

    result = payload["result"]
    key = next(k for k in result if k != "last")
    now = now or time.time()
    candles = []
    for row in result[key]:
        t, close = int(row[0]), float(row[4])
        if t + DAY <= now:  # vain sulkeutuneet päivät
            candles.append((t, close))
    if len(candles) < 200:
        raise RuntimeError(f"Liian vähän kurssihistoriaa ({pair}): {len(candles)} päivää")
    return candles


# ---------------------------------------------------------- indikaattorit

def ema(values, n):
    k = 2 / (n + 1)
    out, p = [], None
    for i, v in enumerate(values):
        p = v if p is None else v * k + p * (1 - k)
        out.append(p if i >= n - 1 else None)
    return out


def sma(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def signals(closes, fast, slow, trend):
    f, s, m = ema(closes, fast), ema(closes, slow), sma(closes, trend)
    out = []
    for i, c in enumerate(closes):
        if f[i] is None or s[i] is None or m[i] is None:
            out.append(None)
        else:
            out.append({"ema_fast": f[i], "ema_slow": s[i], "sma": m[i],
                        "ema_up": f[i] > s[i], "above_trend": c > m[i],
                        "ok": f[i] > s[i] and c > m[i]})
    return out


# ---------------------------------------------------------------- botti

def day_str(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def init_state(cfg, prices):
    st = {"aloitettu": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "salkut": {}}
    for a in cfg["kohteet"]:
        cash = cfg["aloituspaaoma_eur"] * a["osuus"]
        st["salkut"][a["nimi"]] = {
            "kateinen": cash, "maara": 0.0, "ostohinta": None,
            "aloitusarvo": cash, "aloitushinta": prices[a["nimi"]],
            "viimeisin_paiva": None,
        }
    return st


def append_csv(name, header, row):
    new = not os.path.exists(path(name))
    with open(path(name), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(header)
        w.writerow(row)


def fmt(v, d=2):
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")


def pct(v):
    return ("+" if v >= 0 else "−") + fmt(abs(v), 1) + " %"


def run(args):
    cfg = load_json("config.json", None)
    if cfg is None:
        sys.exit("config.json puuttuu")
    fee = cfg["kulu_prosenttia"] / 100

    market = {}
    for a in cfg["kohteet"]:
        candles = fetch_kraken_daily(a["kraken_pari"], args.data_dir, args.now)
        closes = [c for _, c in candles]
        sig = signals(closes, cfg["nopea_ema"], cfg["hidas_ema"], cfg["trendiraja"])
        market[a["nimi"]] = {"t": candles[-1][0], "close": closes[-1], "sig": sig[-1]}

    st = load_json("state.json", None) or init_state(cfg, {k: v["close"] for k, v in market.items()})
    events = []

    for a in cfg["kohteet"]:
        name, m = a["nimi"], market[a["nimi"]]
        p = st["salkut"][name]
        day = day_str(m["t"])
        if p["viimeisin_paiva"] == day:
            print(f"{name}: päivä {day} on jo käsitelty, ei toimenpiteitä")
            continue
        s, price = m["sig"], m["close"]
        action = "pidä"
        if s["ok"] and p["maara"] == 0 and p["kateinen"] > 1:
            qty = p["kateinen"] * (1 - fee) / price
            p.update(maara=qty, kateinen=0.0, ostohinta=price)
            action = "osto"
            why = f"EMA {cfg['nopea_ema']} > EMA {cfg['hidas_ema']} ja kurssi trendirajan ({cfg['trendiraja']} pv) yllä"
            append_csv("kaupat.csv", ["paiva", "kohde", "toimenpide", "hinta_eur", "maara", "arvo_eur", "tulos_pros", "syy"],
                       [day, name, "OSTO", round(price, 2), round(qty, 8), round(qty * price, 2), "", why])
            events.append(f"**{name} OSTO** {fmt(price)} € — {why}")
        elif not s["ok"] and p["maara"] > 0:
            got = p["maara"] * price * (1 - fee)
            res = (price / p["ostohinta"] - 1) * 100 - 2 * cfg["kulu_prosenttia"]
            why = (f"EMA {cfg['nopea_ema']} laski EMA {cfg['hidas_ema']} alle" if not s["ema_up"]
                   else f"kurssi putosi trendirajan ({cfg['trendiraja']} pv) alle")
            append_csv("kaupat.csv", ["paiva", "kohde", "toimenpide", "hinta_eur", "maara", "arvo_eur", "tulos_pros", "syy"],
                       [day, name, "MYYNTI", round(price, 2), round(p["maara"], 8), round(got, 2), round(res, 2), why])
            events.append(f"**{name} MYYNTI** {fmt(price)} € ({pct(res)}) — {why}")
            p.update(maara=0.0, kateinen=got, ostohinta=None)
            action = "myynti"
        value = p["kateinen"] + p["maara"] * price
        p["viimeisin_paiva"] = day
        p["viimeisin_hinta"] = price
        p["arvo"] = value
        append_csv("paivakirja.csv",
                   ["paiva", "kohde", "hinta_eur", "ema_nopea", "ema_hidas", "trendiraja", "ostoehto", "toimenpide", "arvo_eur"],
                   [day, name, round(price, 2), round(s["ema_fast"], 2), round(s["ema_slow"], 2), round(s["sma"], 2),
                    "kyllä" if s["ok"] else "ei", action, round(value, 2)])
        print(f"{name} {day}: hinta {price:.2f} €, ostoehto {'voimassa' if s['ok'] else 'ei'}, toimenpide: {action}, arvo {value:.2f} €")

    save_json("state.json", st)
    write_report(cfg, st, market)

    # ilmoitus GitHub-issueksi (työnkulku lukee tämän tiedoston)
    if events:
        with open(path("ilmoitus.md"), "w", encoding="utf-8") as f:
            f.write("Botti teki tänään seuraavat paperikaupat:\n\n")
            f.write("\n".join(f"- {e}" for e in events))
            f.write("\n\nKatso koko tilanne tiedostosta RAPORTTI.md.\n")
    elif os.path.exists(path("ilmoitus.md")):
        os.remove(path("ilmoitus.md"))


def write_report(cfg, st, market):
    lines = ["# Kryptobotin tilanne", "",
             f"Päivitetty {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC · "
             f"paperikauppa alkoi {st['aloitettu']} · sääntö EMA {cfg['nopea_ema']}/{cfg['hidas_ema']} + "
             f"{cfg['trendiraja']} pv trendiraja · kulu {cfg['kulu_prosenttia']} % per kauppa", "",
             "| Kohde | Hinta € | Ostoehto | Positio | Arvo € | Tuotto | Osta & pidä |",
             "|---|---|---|---|---|---|---|"]
    tot_v = tot_start = 0.0
    for a in cfg["kohteet"]:
        n, p, m = a["nimi"], st["salkut"][a["nimi"]], market[a["nimi"]]
        s, price = m["sig"], m["close"]
        v = p["kateinen"] + p["maara"] * price
        tot_v += v
        tot_start += p["aloitusarvo"]
        cond = "✅ voimassa" if s["ok"] else ("❌ lyhyt trendi laskussa" if not s["ema_up"] else "❌ trendirajan alla")
        pos = f"auki ({fmt(p['ostohinta'])} €)" if p["maara"] > 0 else "käteinen"
        lines.append(f"| {n} | {fmt(price)} | {cond} | {pos} | {fmt(v)} | {pct((v / p['aloitusarvo'] - 1) * 100)} | "
                     f"{pct((price / p['aloitushinta'] - 1) * 100)} |")
    lines.append(f"| **Yhteensä** | | | | **{fmt(tot_v)}** | **{pct((tot_v / tot_start - 1) * 100)}** | |")
    lines += ["", "## Signaalien arvot", "", "| Kohde | EMA nopea | EMA hidas | Trendiraja |", "|---|---|---|---|"]
    for a in cfg["kohteet"]:
        s = market[a["nimi"]]["sig"]
        lines.append(f"| {a['nimi']} | {fmt(s['ema_fast'])} | {fmt(s['ema_slow'])} | {fmt(s['sma'])} |")
    lines += ["", "## Viimeisimmät kaupat", ""]
    if os.path.exists(path("kaupat.csv")):
        with open(path("kaupat.csv"), encoding="utf-8") as f:
            rows = list(csv.reader(f, delimiter=";"))[1:]
        if rows:
            lines += ["| Päivä | Kohde | Toimenpide | Hinta € | Tulos | Syy |", "|---|---|---|---|---|---|"]
            for r in reversed(rows[-10:]):
                lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {fmt(float(r[3]))} | {pct(float(r[6])) if r[6] else ''} | {r[7]} |")
        else:
            lines.append("Ei kauppoja vielä.")
    else:
        lines.append("Ei kauppoja vielä.")
    lines += ["", "_Paperikauppa: mitään oikeaa rahaa ei liiku. Menneet tulokset eivät takaa tulevia._", ""]
    with open(path("RAPORTTI.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# -------------------------------------------------------------- backtest

def backtest(args):
    cfg = load_json("config.json", None)
    fee = cfg["kulu_prosenttia"] / 100
    for a in cfg["kohteet"]:
        candles = fetch_kraken_daily(a["kraken_pari"], args.data_dir, args.now)
        closes = [c for _, c in candles]
        sig = signals(closes, cfg["nopea_ema"], cfg["hidas_ema"], cfg["trendiraja"])
        start = next(i for i, s in enumerate(sig) if s is not None)
        eq, pos, trades, peak, dd = 1.0, 0, 0, 1.0, 0.0
        for i in range(start + 1, len(closes)):
            r = closes[i] / closes[i - 1] - 1
            want = 1 if sig[i - 1]["ok"] else 0
            if want != pos:
                eq *= 1 - fee
                trades += 1
                pos = want
            eq *= 1 + pos * r
            peak = max(peak, eq)
            dd = max(dd, 1 - eq / peak)
        hodl = closes[-1] / closes[start] - 1
        print(f"{a['nimi']}: {day_str(candles[start][0])} – {day_str(candles[-1][0])}  "
              f"strategia {pct((eq - 1) * 100)} (pahin lasku −{dd * 100:.0f} %, {trades} kauppaa), "
              f"osta ja pidä {pct(hodl * 100)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--data-dir", help="testikäyttö: lue kurssit tiedostoista")
    ap.add_argument("--now", type=float, help="testikäyttö: kuviteltu nykyhetki (unix-aika)")
    args = ap.parse_args()
    backtest(args) if args.backtest else run(args)
