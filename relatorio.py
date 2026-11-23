"""Gera o texto do relatório de suprimentos (o mesmo do botão 📋 Relatório da página)."""
import re, unicodedata
from datetime import datetime

COR = {"black": "Preto", "cyan": "Ciano", "magenta": "Magenta", "yellow": "Amarelo", "preto": "Preto"}
SEDE = ["UN1", "UN2", "UN5", "SECAD", "CEMETRO", "CLINICASMEDVET", "CLINICA", "UN5/TEMPORARIA"]
SIGLAS = ["RH", "TI", "EAD", "ZL", "CPD", "NADI"]
MINUSC = ["DE", "DA", "DO", "DAS", "DOS", "E"]


def nrm(s):
    s = unicodedata.normalize("NFD", s or "")
    return re.sub(r"\s+", "", "".join(c for c in s if unicodedata.category(c) != "Mn").upper())


def guess(m):
    m = (m or "").strip()
    if re.match(r"(?i)(brother|dcp)", m): return "Brother"
    if re.search(r"(?i)epson", m) or re.match(r"(?i)wf-", m): return "Epson"
    if re.match(r"(?i)hp", m): return "HP"
    if re.match(r"(?i)(oki|es\d)", m): return "OKI"
    if re.match(r"(?i)ricoh", m): return "Ricoh"
    if re.match(r"(?i)savin", m): return "Savin"
    return ""


def marca(p):
    return p.get("marca") or guess(p.get("modelo"))


def cor(i):
    m = re.search(r"black|cyan|magenta|yellow|preto", ((i.get("cor") or "") + " " + (i.get("nome") or "")).lower())
    return COR[m.group(0)] if m else ""


def sups(p, k):
    t = "toner" if k == "toner" else "cilindro"
    n, l = p.get("ndd"), p.get("leitura")
    a = [i for i in (n or {}).get("suprimentos", []) if i.get("tipo") == t]
    b = [i for i in (l or {}).get("suprimentos", []) if i.get("tipo") == t] if l and l.get("online") else []
    if a and b:
        return b if (l.get("lido_em") or "") > (n.get("lido_em") or "") else a
    return a or b


def lvl(p, k):
    v = [i["pct"] for i in sups(p, k) if i.get("pct") is not None]
    return min(v) if v else None


def nivel(p):
    v = [x for x in (lvl(p, "toner"), lvl(p, "cil")) if x is not None]
    return min(v) if v else None


def txs(p, k):
    its = [i for i in sups(p, k) if i.get("pct") is not None]
    if not its:
        return "—"
    return " / ".join((cor(i).lower() + " " if cor(i) and (k == "toner" or len(its) > 1) else "") + f"{i['pct']}%" for i in its)


def tcase(s):
    if not s or s != s.upper():
        return s or ""
    out = []
    for w in s.split():
        if w in MINUSC: out.append(w.lower())
        elif re.search(r"\d", w) or w in SIGLAS: out.append(w)
        else: out.append(w[0] + w[1:].lower())
    return " ".join(out)


def lido(p):
    return (p.get("ndd") or {}).get("lido_em") or (p.get("leitura") or {}).get("lido_em") or ""


def velha(p):
    l = lido(p)
    try:
        return bool(l) and (datetime.now() - datetime.fromisoformat(l)).total_seconds() > 36 * 3600
    except ValueError:
        return False


def fmt(s):
    try:
        return datetime.fromisoformat(s).strftime("%d/%m, %H:%M")
    except ValueError:
        return ""


def linha(p, mk):
    ps = []
    tn, cl = txs(p, "toner"), txs(p, "cil")
    if tn != "—": ps.append("Toner " + tn)
    if cl != "—": ps.append("Cilindro " + cl)
    its = sorted([i for i in sups(p, "toner") + sups(p, "cil") if i.get("pct") is not None], key=lambda i: i["pct"])
    m = re.match(r"^(\d{2}/\d{2})/\d{4}$", its[0].get("previsao") or "") if its else None
    return ("• *" + tcase(p.get("setor") or "Sem setor") + "*" + (f" ({p['local']})" if p.get("local") else "") + ": "
            + (" · ".join(ps) or "sem leitura") + (f" ⏳ {m.group(1)}" if m else "")
            + (" ⚠️ leitura antiga" if mk and velha(p) else ""))


def filtrar(printers, limite=None, unidade="", setor="", q=""):
    out = []
    for p in printers:
        if unidade:
            if unidade.upper() in ("SEDE", "__SEDE__"):
                if nrm(p.get("local")) not in SEDE: continue
            elif nrm(p.get("local")) != nrm(unidade): continue
        if setor and p.get("setor") != setor: continue
        if q:
            hay = " ".join([marca(p), p.get("setor") or "", p.get("local") or "", p.get("ip") or "", p.get("modelo") or "",
                            p.get("mac") or "", p.get("serie") or ""]).lower()
            if q.lower() not in hay: continue
        if limite is not None and not any(v is not None and v <= limite for v in (lvl(p, "toner"), lvl(p, "cil"))):
            continue
        out.append(p)
    return out


def relatorio(printers, limite=None, unidade="", setor="", q=""):
    L = sorted([p for p in filtrar(printers, limite, unidade, setor, q) if nivel(p) is not None], key=nivel)
    f = []
    if limite is not None: f.append(f"Nível instalado ≤ {limite}%")
    if unidade: f.append("SEDE" if unidade.upper() in ("SEDE", "__SEDE__") else unidade.upper())
    if setor: f.append(setor)
    if q: f.append(f'busca "{q}"')
    ds = sorted(x for x in (lido(p) for p in L) if x)
    tv = sum(1 for p in L if velha(p))
    todas = tv > 0 and tv == len(ds)
    t = ("📋 *RELATÓRIO DE SUPRIMENTOS*\n"
         + ("🗓️ Leitura: " + fmt(ds[-1]) if ds else "🗓️ Emitido em " + datetime.now().strftime("%d/%m/%Y"))
         + "\n🔎 Filtro: " + (" · ".join(f) or "todas as impressoras") + "\n"
         + ("⚠️ Leituras com mais de 36 h. Reimporte o NDD para atualizar.\n" if todas else ""))
    faixas = [("🔴", "CRÍTICO (até 5%)", lambda v: v <= 5), ("🟠", "ALTO (6 a 10%)", lambda v: 5 < v <= 10),
              ("🟡", "ATENÇÃO (11 a 20%)", lambda v: 10 < v <= 20), ("🟢", "OK (acima de 20%)", lambda v: v > 20)]
    cont = []
    for e, nome, fn in faixas:
        g = [p for p in L if fn(nivel(p))]
        cont.append(len(g))
        if g:
            t += f"\n{e} *{nome}* — {len(g)}\n" + "\n".join(linha(p, not todas) for p in g) + "\n"
    t += (f"\n📦 *RESUMO:* {len(L)} impressoras · 🔴 {cont[0]} · 🟠 {cont[1]} · 🟡 {cont[2]}" + (f" · 🟢 {cont[3]}" if cont[3] else ""))
    return t
