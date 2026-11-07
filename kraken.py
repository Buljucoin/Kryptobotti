"""
Kraken-yhteys kryptobotille.

Tilat (config.json -> "kraken" -> "tila"):
  "pois"   ei yhteyttä Krakeniin, vain paperikauppa
  "kuiva"  kuivaharjoitus: lukee oikean saldon ja tarkistuttaa Krakenilla
           toimeksiannon, jonka botti tekisi (validate=true), mutta EI lähetä sitä
  "oikea"  oikeat kaupat Krakenissa turvarajojen sisällä

Botti käyttää vain omaa budjettiaan (paaoma_eur) eikä koske tilin muihin varoihin:
se myy vain sen määrän, jonka se on itse ostanut.

API-avain luetaan ympäristömuuttujista KRAKEN_API_KEY ja KRAKEN_API_SECRET
(GitHubin salaisuudet). Avaimelle EI saa antaa nosto-oikeutta.
"""
import base64
import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse
import urllib.request

API = "https://api.kraken.com"


class KrakenError(RuntimeError):
    pass


def sign(path, data, secret, nonce):
    """Krakenin API-Sign: HMAC-SHA512(uri_path + SHA256(nonce + postdata)), avaimena base64-dekoodattu secret."""
    postdata = urllib.parse.urlencode(data)
    message = path.encode() + hashlib.sha256((str(nonce) + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class Kraken:
    """Ohut rajapintakerros. Testit korvaavat tämän valeversiolla."""

    def __init__(self, key, secret, sleep=time.sleep):
        self.key, self.secret, self.sleep = key, secret, sleep
        self._nonce = 0

    def _nonce_next(self):
        self._nonce = max(self._nonce + 1, int(time.time() * 1000))
        return self._nonce

    def _request(self, url, body=None, headers=None):
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "kryptobotti/2.0", **(headers or {})})
                with urllib.request.urlopen(req, timeout=30) as r:
                    payload = json.load(r)
                if payload.get("error"):
                    raise KrakenError(", ".join(payload["error"]))
                return payload["result"]
            except KrakenError:
                raise  # rajapinnan virhe: ei toisteta (esim. kauppa voisi mennä kahdesti)
            except Exception as e:
                last = e
                if body is not None:
                    break  # yksityistä kutsua ei toisteta verkkovirheen jälkeen
                self.sleep(3 * (attempt + 1))
        raise KrakenError(f"yhteysvirhe: {last}")

    def public(self, method, params=None):
        q = "?" + urllib.parse.urlencode(params) if params else ""
        return self._request(f"{API}/0/public/{method}{q}")

    def private(self, method, params=None):
        path = f"/0/private/{method}"
        data = {"nonce": self._nonce_next(), **(params or {})}
        headers = {"API-Key": self.key, "API-Sign": sign(path, data, self.secret, data["nonce"]),
                   "Content-Type": "application/x-www-form-urlencoded"}
        return self._request(API + path, urllib.parse.urlencode(data).encode(), headers)


# --------------------------------------------------------------- apurit

def floor_dec(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def fnum(x, decimals):
    return f"{x:.{decimals}f}"


def pair_info(api, pair):
    res = api.public("AssetPairs", {"pair": pair})
    key, info = next(iter(res.items()))
    return {"key": key, "base": info["base"], "quote": info["quote"],
            "lot": int(info["lot_decimals"]), "px": int(info["pair_decimals"]),
            "ordermin": float(info.get("ordermin", 0) or 0), "costmin": float(info.get("costmin", 0) or 0)}


def ticker(api, pi, pair):
    res = api.public("Ticker", {"pair": pair})
    t = res.get(pi["key"]) or next(iter(res.values()))
    return float(t["b"][0]), float(t["a"][0])


def balances(api):
    return {k: float(v) for k, v in api.private("Balance").items()}


def query(api, txid):
    o = api.private("QueryOrders", {"txid": txid, "trades": "false"})[txid]
    return {"status": o["status"], "vol": float(o["vol"]), "vol_exec": float(o["vol_exec"]),
            "cost": float(o["cost"]), "fee": float(o["fee"]), "reason": o.get("reason")}


def wait_closed(api, txid, tries=10, delay=2):
    for _ in range(tries):
        o = query(api, txid)
        if o["status"] in ("closed", "canceled", "expired"):
            return o
        api.sleep(delay)
    return query(api, txid)


def execute(api, pair, pi, side, volume, kcfg, log):
    """Toteuttaa toimeksiannon: ensin post-only-limiitti (maker-kulu) parhaaseen hintaan,
    hinta päivitetään minuutin välein. Jos kaikki ei täyty aikarajassa, loput markkinahinnalla.
    Palauttaa toteutuneen määrän, hinnan (EUR), kulun ja txid:t."""
    filled = cost = fee = 0.0
    txids = []
    remaining = volume
    for _ in range(max(1, int(kcfg.get("tayttoaika_min", 15)))):  # yksi yritys minuutissa
        if remaining < pi["ordermin"]:
            break
        bid, ask = ticker(api, pi, pair)
        price = bid if side == "buy" else ask
        res = api.private("AddOrder", {"pair": pair, "type": side, "ordertype": "limit",
                                       "price": fnum(price, pi["px"]), "volume": fnum(remaining, pi["lot"]),
                                       "oflags": "post", "userref": 2026})
        txid = res["txid"][0]
        txids.append(txid)
        log(f"  limiitti {side} {fnum(remaining, pi['lot'])} @ {fnum(price, pi['px'])} ({txid})")
        api.sleep(60)
        o = query(api, txid)
        if o["status"] == "open":
            api.private("CancelOrder", {"txid": txid})
            o = wait_closed(api, txid)
        filled += o["vol_exec"]
        cost += o["cost"]
        fee += o["fee"]
        remaining = floor_dec(volume - filled, pi["lot"])
    if remaining >= pi["ordermin"] and kcfg.get("varalla_markkinahinta", True):
        res = api.private("AddOrder", {"pair": pair, "type": side, "ordertype": "market",
                                       "volume": fnum(remaining, pi["lot"]), "userref": 2026})
        txid = res["txid"][0]
        txids.append(txid)
        log(f"  markkinatoimeksianto {side} {fnum(remaining, pi['lot'])} ({txid})")
        o = wait_closed(api, txid)
        filled += o["vol_exec"]
        cost += o["cost"]
        fee += o["fee"]
    return {"maara": filled, "hinta": cost / filled if filled else 0.0, "kustannus": cost, "kulu": fee, "txid": txids}


# ------------------------------------------------------------ päivän ajo

def init_kstate(cfg, kcfg):
    return {"aloitettu": None, "kauppoja": {},
            "salkut": {a["nimi"]: {"budjetti_eur": kcfg["paaoma_eur"] * a["osuus"], "maara": 0.0, "ostohinta": None}
                       for a in cfg["kohteet"]}}


def step(cfg, st, market, today, api=None, env=os.environ, log=print, record=None):
    """Ajaa päivän Kraken-osuuden. Palauttaa {'tila', 'rivit' (raporttiin), 'ilmoitukset', 'virhe'}."""
    kcfg = cfg.get("kraken") or {"tila": "pois"}
    mode = kcfg.get("tila", "pois")
    out = {"tila": mode, "rivit": [], "ilmoitukset": [], "virhe": None}
    if mode == "pois":
        return out
    if mode not in ("kuiva", "oikea"):
        out["virhe"] = f"tuntematon tila '{mode}' (sallitut: pois, kuiva, oikea)"
        return out
    if api is None:
        key, secret = env.get("KRAKEN_API_KEY", ""), env.get("KRAKEN_API_SECRET", "")
        if not key or not secret:
            out["rivit"].append("API-avainta ei ole tallennettu GitHubin salaisuuksiin (KRAKEN_API_KEY ja KRAKEN_API_SECRET), "
                                "joten Krakeniin ei otettu yhteyttä.")
            return out
        api = Kraken(key, secret)

    ks = st.setdefault("kraken", init_kstate(cfg, kcfg))
    ks["aloitettu"] = ks["aloitettu"] or today
    for a in cfg["kohteet"]:  # uusi kohde configiin -> oma budjetti
        ks["salkut"].setdefault(a["nimi"], {"budjetti_eur": kcfg["paaoma_eur"] * a["osuus"], "maara": 0.0, "ostohinta": None})

    try:
        bal = balances(api)
        infos = {a["nimi"]: pair_info(api, a["kraken_pari"]) for a in cfg["kohteet"]}
        eur_key = infos[cfg["kohteet"][0]["nimi"]]["quote"]
        eur = bal.get(eur_key, 0.0)
        out["rivit"].append(f"Tilin saldo: {eur:.2f} € käteistä" + "".join(
            f", {bal.get(infos[n]['base'], 0.0):.8f} {n}" for n in infos) + ".")
        done_today = ks["kauppoja"].get(today, 0)

        for a in cfg["kohteet"]:
            name, pair, pi = a["nimi"], a["kraken_pari"], infos[a["nimi"]]
            p, m = ks["salkut"][name], market[name]
            price, ok = m["close"], m["sig"]["ok"]
            has_pos = p["maara"] >= pi["ordermin"]
            if ok and not has_pos:
                side = "buy"
                spend = min(p["budjetti_eur"], eur, kcfg.get("max_kauppa_eur", float("inf")))
                margin = cfg["kulu_prosenttia"] / 100 + 0.005  # kulu + liikkumavara hinnalle
                price = max(price, ticker(api, pi, pair)[1])  # koko lasketaan nykyisellä myyntitarjouksella
                volume = floor_dec(spend * (1 - margin) / price, pi["lot"])
            elif not ok and has_pos:
                side = "sell"
                volume = floor_dec(min(p["maara"], bal.get(pi["base"], 0.0)), pi["lot"])
            else:
                tila_txt = f"positio auki ({p['maara']:.8f} {name})" if has_pos else "ei positiota"
                out["rivit"].append(f"{name}: ei toimenpidettä, {tila_txt}, budjetti {p['budjetti_eur']:.2f} €.")
                continue

            verb = "osto" if side == "buy" else "myynti"
            if volume < pi["ordermin"] or volume * price < pi["costmin"]:
                msg = (f"{name}: {verb} jäi tekemättä, koska määrä {volume:.8f} on alle Krakenin vähimmäiskoon "
                       f"({pi['ordermin']} {name}). Tarkista käteinen ja budjetti.")
                out["rivit"].append(msg)
                out["ilmoitukset"].append(msg)
                continue
            if done_today >= kcfg.get("max_kauppoja_paivassa", 2):
                msg = f"{name}: {verb} jäi tekemättä, koska päivän kauppamäärän raja täyttyi."
                out["rivit"].append(msg)
                out["ilmoitukset"].append(msg)
                continue

            if mode == "kuiva":
                bid, ask = ticker(api, pi, pair)
                lim = bid if side == "buy" else ask
                api.private("AddOrder", {"pair": pair, "type": side, "ordertype": "limit",
                                         "price": fnum(lim, pi["px"]), "volume": fnum(volume, pi["lot"]),
                                         "oflags": "post", "validate": "true"})
                msg = (f"**{name} {verb.upper()} (kuivaharjoitus)**: botti olisi lähettänyt limiittitoimeksiannon "
                       f"{fnum(volume, pi['lot'])} {name} @ {fnum(lim, pi['px'])} € (noin {volume * lim:.2f} €). "
                       f"Kraken hyväksyi toimeksiannon tarkistuksen, mutta sitä ei lähetetty.")
                out["rivit"].append(msg)
                out["ilmoitukset"].append(msg)
                continue

            # oikea kauppa
            res = execute(api, pair, pi, side, volume, kcfg, log)
            done_today += 1
            ks["kauppoja"] = {today: done_today}
            if res["maara"] <= 0:
                msg = f"**{name} {verb.upper()}**: toimeksianto ei toteutunut. Botti yrittää uudelleen huomenna."
                out["rivit"].append(msg)
                out["ilmoitukset"].append(msg)
                continue
            if side == "buy":
                p["maara"] += res["maara"]
                p["budjetti_eur"] -= res["kustannus"] + res["kulu"]
                p["ostohinta"] = res["hinta"]
                eur -= res["kustannus"] + res["kulu"]
                tulos = ""
            else:
                ostohinta = p["ostohinta"]
                p["maara"] = max(0.0, p["maara"] - res["maara"])
                p["budjetti_eur"] += res["kustannus"] - res["kulu"]
                eur += res["kustannus"] - res["kulu"]
                tulos = f" ({(res['hinta'] / ostohinta - 1) * 100:+.1f} % ennen kuluja)" if ostohinta else ""
                if p["maara"] < pi["ordermin"]:
                    p["ostohinta"] = None
            if record:
                record([today, name, verb.upper(), round(res["hinta"], 2), round(res["maara"], 8),
                        round(res["kustannus"], 2), round(res["kulu"], 2), " ".join(res["txid"])])
            msg = (f"**{name} {verb.upper()} (oikea kauppa)**: {res['maara']:.8f} {name} @ {res['hinta']:.2f} €, "
                   f"yhteensä {res['kustannus']:.2f} €, kulu {res['kulu']:.2f} €{tulos}.")
            if res["maara"] < volume * 0.999:
                msg += f" Huom: vain osa ({res['maara']:.8f}/{volume:.8f}) toteutui."
            out["rivit"].append(msg)
            out["ilmoitukset"].append(msg)
    except Exception as e:
        out["virhe"] = str(e)
    return out
