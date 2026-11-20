"""Veille YouTube : repère les « pépites » de ton thème.

Une pépite = une vidéo qui a fait beaucoup plus de vues que sa chaîne n'a d'abonnés.
Lancement (depuis la racine du projet, environnement « Youtube » qui autorise youtube.com) :
    python3 outils/veille.py
Résultat : la liste des pépites à l'écran, et le détail dans outils/veille-resultats.json.
"""
import concurrent.futures as cf
import json
import re
import subprocess
import urllib.parse
from pathlib import Path

ICI = Path(__file__).parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
TRI_PAR_VUES = "CAM%253D"


def telecharger(url, langue):
    r = subprocess.run(["curl", "-s", "-A", UA, "-H", f"Accept-Language: {langue}", url],
                       capture_output=True, text=True, timeout=60)
    return r.stdout


def donnees(html):
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
    return json.loads(m.group(1)) if m else {}


def parcourir(o, cle):
    if isinstance(o, dict):
        if cle in o:
            yield o[cle]
        for v in o.values():
            yield from parcourir(v, cle)
    elif isinstance(o, list):
        for v in o:
            yield from parcourir(v, cle)


def texte(t):
    if not t:
        return ""
    return t.get("simpleText") or "".join(r.get("text", "") for r in t.get("runs", []))


def nombre(s):
    chiffres = re.sub(r"\D", "", s)
    return int(chiffres) if chiffres else 0


def chercher(requete, langue, tri):
    hl, gl = ("fr", "FR") if langue.startswith("fr") else ("en", "US")
    url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote(requete)}"
           f"&hl={hl}&gl={gl}" + (f"&sp={tri}" if tri else ""))
    videos = []
    for v in parcourir(donnees(telecharger(url, langue)), "videoRenderer"):
        chaine = v.get("ownerText", {}).get("runs", [{}])[0]
        videos.append(dict(
            id=v["videoId"], titre=texte(v.get("title")), vues=nombre(texte(v.get("viewCountText"))),
            chaine=chaine.get("text", ""),
            chaine_id=chaine.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", ""),
            age=texte(v.get("publishedTimeText")), requete=requete, langue=hl))
    return videos


def abonnes(chaine_id):
    html = telecharger(f"https://www.youtube.com/channel/{chaine_id}?hl=en&gl=US", "en-US,en;q=0.9")
    entete = json.dumps(donnees(html).get("header", {}), ensure_ascii=False)
    m = re.search(r"([\d.,]+)\s*([KMB]?) subscribers", entete)
    if not m:
        return None
    return int(float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def main():
    requetes = json.loads((ICI / "requetes.json").read_text())
    taches = [(q, langue, tri) for langue, qs in requetes.items() for q in qs for tri in ("", TRI_PAR_VUES)]
    videos = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for resultat in ex.map(lambda t: chercher(*t), taches):
            for v in resultat:
                videos.setdefault(v["id"], v)
    chaines = sorted({v["chaine_id"] for v in videos.values() if v["chaine_id"]})
    with cf.ThreadPoolExecutor(8) as ex:
        nb_abonnes = dict(zip(chaines, ex.map(abonnes, chaines)))
    for v in videos.values():
        v["abonnes"] = nb_abonnes.get(v["chaine_id"])
        v["ratio"] = round(v["vues"] / v["abonnes"], 1) if v["abonnes"] else None
    (ICI / "veille-resultats.json").write_text(json.dumps(list(videos.values()), ensure_ascii=False, indent=1))

    pepites = [v for v in videos.values()
               if v["ratio"] and v["ratio"] >= 3 and v["abonnes"] <= 500_000 and v["vues"] >= 50_000]
    pepites.sort(key=lambda v: -v["ratio"])
    print(f"{len(videos)} vidéos, {len(chaines)} chaînes, {len(pepites)} pépites\n")
    for v in pepites[:80]:
        print(f"{v['ratio']:>7}x | {v['vues']:>10,} vues | {v['abonnes']:>7,} ab. | {v['age'][:11]:11} | "
              f"{v['chaine'][:20]:20} | {v['titre'][:90]} | https://youtu.be/{v['id']}")


if __name__ == "__main__":
    main()
