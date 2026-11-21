"""Veille YouTube francophone : repère les vidéos qui explosent dans ton thème.

Une « pépite » = une vidéo qui fait beaucoup plus de vues que d'habitude POUR SA CHAÎNE
(vues de la vidéo ÷ vues médianes des 30 dernières vidéos de la chaîne).

Méthode :
1. Recherches YouTube en français (outils/requetes.json), triées par pertinence, par vues et sur l'année.
2. Garde les chaînes vraiment francophones : titres d'origine lus dans le flux RSS
   (YouTube traduit les titres à l'affichage, donc la page seule ne suffit pas).
3. Garde les petites et moyennes chaînes (1 000 à 500 000 abonnés).
4. Calcule le score de chaque vidéo récente (moins de 2 ans) et garde celles de ton thème.

Lancement, depuis la racine du projet, dans l'environnement cloud « Youtube » (qui autorise youtube.com) :
    python3 outils/veille.py
Durée : 4 à 6 minutes. Résultat : la liste à l'écran, le détail dans outils/veille-resultats.json.
"""
import concurrent.futures as cf
import html as html_lib
import json
import re
import statistics
import subprocess
import urllib.parse
from pathlib import Path

ICI = Path(__file__).parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
LANGUE = "fr-FR,fr;q=0.9"
TRIS = ["", "CAM%253D", "EgIIBQ%253D%253D"]  # pertinence, nombre de vues, cette année
ABONNES_MIN, ABONNES_MAX = 1_000, 500_000
SCORE_MIN, VUES_MIN = 3, 20_000

MOTS_FR = set("le la les de des du un une et tu te ton ta tes je j vous votre vos pour pas ce cette qui que est à comment "
              "pourquoi avec sans mon ma mes son sa ses on nous ne n plus quand tout faire être avoir si ça c d l qu".split())
MOTS_EN = set("the you your to and of how why is are my i it this that with for in what i'm don't can will".split())

THEME = ["procrastin", "motiv", "flemm", "paress", "fainéant", "discipline", "cerveau", "dopamine", "système nerveux",
         "nerf vague", "anxi", "peur", "regard des autres", "jugé", "jugement", "imposteur", "légitim", "échec", "échoue",
         "perfection", "oser", "confiance en soi", "passion", "ta voie", "sa voie", "ikigai", "sens de", "burn", "épuis",
         "intuition", "âme", "créativ", "créer", "créat", "artiste", "l'art ", "peindre", "dessin", "ennui", "ennuie",
         "reconversion", "multipotent", "zèbre", "hpi", "surdoué", "touche-à-tout", "touche à tout", "slash", "tdah",
         "hyperfocus", "hyperactiv", "dispers", "attention", "hypersensib", "ambiti", "rêve", "perdu", "vingtaine",
         "trentaine", "30 ans", "scroll", "habitude", "changer de vie", "passer à l'action", "réussir", "mental",
         "bonheur", "heureux", "émotion", "réfléchi", "penser", "pensée", "estime de soi", "psycholog", "mindset",
         "se lancer", "te lancer", "projet", "concentr", "solitude", "j'ai arrêté", "quitter", "personne ne",
         "regarde cette vidéo", "si tu es", "tu n'es pas", "ta vie", "ma vie", "soi-même", "toi-même", "plaire",
         "masque", "corps", "respir", "marche", "stress", "fatigue", "joie", "envie", "talent", "spiritu", "éveil",
         "énergie", "synchronicit", "lâcher", "mission de vie", "enfant intérieur", "quête"]
HORS_SUJET = ["clip", "official", "officiel", "lyrics", "audio", "music", "musique", "chant", "feat", "ft.", "karaoké",
              "remix", "bande-annonce", "teaser", "spot", "louange", "gospel", "asmr", "livre audio", "hypnose",
              "sommeil", "dormir", "comptine", "film", "épisode", "pdg", "méditation guidée", "sans pub"]


def telecharger(url):
    r = subprocess.run(["curl", "-s", "-A", UA, "-H", f"Accept-Language: {LANGUE}", url],
                       capture_output=True, text=True, timeout=60)
    return r.stdout


def donnees(page):
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page)
    try:
        return json.loads(m.group(1)) if m else {}
    except json.JSONDecodeError:
        return {}


def parcourir(o, cle):
    if isinstance(o, dict):
        if cle in o:
            yield o[cle]
        for v in o.values():
            yield from parcourir(v, cle)
    elif isinstance(o, list):
        for v in o:
            yield from parcourir(v, cle)


def nombre_fr(s):
    """'331 k' -> 331000 ; '4,1 k' -> 4100 ; '1,2 M' -> 1200000 ; '950' -> 950."""
    s = s.replace("\xa0", " ").replace(" ", " ")
    m = re.search(r"(\d[\d ]*(?:,\d+)?)\s*(Md|k|M)?", s)
    if not m:
        return None
    valeur = float(m.group(1).replace(" ", "").replace(",", "."))
    return int(valeur * {None: 1, "k": 1e3, "M": 1e6, "Md": 1e9}[m.group(2)])


def est_francais(titre):
    mots = re.findall(r"[a-zà-ÿ']+", titre.lower().replace("’", "'"))
    mots = [m.split("'")[0] for m in mots]
    fr = sum(m in MOTS_FR for m in mots) + 2 * bool(re.search(r"[éèêàçù]", titre.lower()))
    en = sum(m in MOTS_EN for m in mots)
    return fr > en and fr >= 1


def dans_le_theme(titre):
    t = titre.lower().replace("’", "'")
    return any(k in t for k in THEME) and not any(k in t for k in HORS_SUJET)


def recent(age):
    """'il y a 8 m.' ou 'il y a 2 a' -> moins de 2 ans."""
    m = re.search(r"(\d+)\s*a\b", age)
    return not (m and int(m.group(1)) > 2)


def chercher(requete, tri):
    url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote(requete)}&hl=fr&gl=FR"
           + (f"&sp={tri}" if tri else ""))
    ids = []
    for v in parcourir(donnees(telecharger(url)), "videoRenderer"):
        chaine = v.get("ownerText", {}).get("runs", [{}])[0]
        cid = chaine.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")
        if cid:
            ids.append(cid)
    return ids


def part_de_francais(cid):
    xml = telecharger(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    titres = [html_lib.unescape(t) for t in re.findall(r"<media:title>(.*?)</media:title>", xml)]
    return sum(map(est_francais, titres)) / len(titres) if titres else 0


def chaine(cid):
    d = donnees(telecharger(f"https://www.youtube.com/channel/{cid}/videos?hl=fr&gl=FR"))
    entete = json.dumps(d.get("header", {}), ensure_ascii=False)
    m = re.search(r"([\d\s \xa0,]+(?:k|M)?)\s*abonnés", entete)
    nom = next(parcourir(d.get("metadata", {}), "title"), "")
    videos = []
    for l in parcourir(d, "lockupViewModel"):
        if l.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        md = l.get("metadata", {}).get("lockupMetadataViewModel", {})
        parts = [p for lignes in parcourir(md, "metadataRows") for ligne in lignes for p in ligne.get("metadataParts", [])]
        vues = next((nombre_fr(p["text"]["content"]) for p in parts if "vue" in p.get("accessibilityLabel", "")), None)
        age = next((p["text"]["content"] for p in parts if "il y a" in p.get("text", {}).get("content", "")), "")
        if vues is not None:
            videos.append(dict(id=l["contentId"], titre=md.get("title", {}).get("content", ""), vues=vues, age=age))
    return dict(cid=cid, nom=nom, abonnes=nombre_fr(m.group(1)) if m else None, videos=videos)


def main():
    requetes = json.loads((ICI / "requetes.json").read_text())
    taches = [(angle, q, tri) for angle, qs in requetes.items() for q in qs for tri in TRIS]
    angles = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for (angle, _, _), cids in zip(taches, ex.map(lambda t: chercher(t[1], t[2]), taches)):
            for cid in cids:
                angles.setdefault(cid, {}).setdefault(angle, 0)
                angles[cid][angle] += 1
    cids = sorted(angles)
    with cf.ThreadPoolExecutor(12) as ex:
        francais = [c for c, part in zip(cids, ex.map(part_de_francais, cids)) if part >= 0.6]
    with cf.ThreadPoolExecutor(10) as ex:
        chaines = list(ex.map(chaine, francais))

    pepites = []
    for c in chaines:
        if not c["abonnes"] or not ABONNES_MIN <= c["abonnes"] <= ABONNES_MAX or len(c["videos"]) < 10:
            continue
        mediane = statistics.median(v["vues"] for v in c["videos"]) or 1
        angle = max(angles[c["cid"]], key=angles[c["cid"]].get)
        for v in c["videos"]:
            score = v["vues"] / mediane
            if score >= SCORE_MIN and v["vues"] >= VUES_MIN and recent(v["age"]) and dans_le_theme(v["titre"]):
                pepites.append(dict(score=round(score, 1), vues=v["vues"], abonnes=c["abonnes"], mediane=int(mediane),
                                    age=v["age"], chaine=c["nom"], titre=v["titre"], angle=angle,
                                    lien=f"https://youtu.be/{v['id']}", id=v["id"]))
    pepites.sort(key=lambda p: -p["score"])
    (ICI / "veille-resultats.json").write_text(json.dumps(pepites, ensure_ascii=False, indent=1))

    print(f"{len(cids)} chaînes trouvées, {len(francais)} francophones, {len(pepites)} pépites\n")
    for p in pepites:
        print(f"{p['score']:>6}x | {p['vues']:>9,} vues | {p['abonnes']:>7,} ab. | {p['age'][:12]:12} | "
              f"{p['angle'][:14]:14} | {p['chaine'][:22]:22} | {p['titre'][:95]} | {p['lien']}")


if __name__ == "__main__":
    main()
