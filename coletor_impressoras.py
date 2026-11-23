#!/usr/bin/env python3
"""
Coletor de suprimentos de impressoras via SNMP (sem dependências externas).

Uso rápido:
    python coletor_impressoras.py                      # lê ips.txt, grava leitura.json
    python coletor_impressoras.py -e impressoras.json  # usa o backup exportado do sistema
    python coletor_impressoras.py -e planilha.xlsx     # usa a planilha
    python coletor_impressoras.py -c minhacomunidade   # comunidade SNMP diferente de "public"
    python coletor_impressoras.py --a-cada 60          # repete a cada 60 minutos

Depois, no sistema web, use o botão "Importar" e escolha o leitura.json.
Rode em um computador que enxergue a rede das impressoras (192.168.x.x).
"""
import argparse, json, os, random, re, socket, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

END = object()  # endOfMibView / noSuchObject

# ----------------------------- SNMP (BER) mínimo -----------------------------
def _len(n):
    if n < 128:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b

def _tlv(t, v):
    return bytes([t]) + _len(len(v)) + v

def _int(i):
    n = 1
    while True:
        try:
            return _tlv(2, i.to_bytes(n, "big", signed=True))
        except OverflowError:
            n += 1

def _oid(s):
    p = [int(x) for x in s.strip(".").split(".")]
    out = bytearray([40 * p[0] + p[1]])
    for x in p[2:]:
        c = [x & 0x7F]
        x >>= 7
        while x:
            c.append(0x80 | (x & 0x7F))
            x >>= 7
        out += bytes(reversed(c))
    return _tlv(6, bytes(out))

def _oid_str(c):
    if not c:
        return ""
    parts, v = [c[0] // 40, c[0] % 40], 0
    for b in c[1:]:
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))

def _read(d, i):
    t, l = d[i], d[i + 1]
    i += 2
    if l & 0x80:
        n = l & 0x7F
        l = int.from_bytes(d[i:i + n], "big")
        i += n
    return t, d[i:i + l], i + l

def _children(v):
    out, i = [], 0
    while i < len(v):
        t, c, i = _read(v, i)
        out.append((t, c))
    return out

def _val(t, c):
    if t == 2:
        return int.from_bytes(c, "big", signed=True) if c else 0
    if t in (0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(c, "big")
    if t == 4:
        return bytes(c)
    if t == 6:
        return _oid_str(c)
    if t in (0x80, 0x81, 0x82):
        return END
    return None

def _request(ver, comm, tag, rid, oid):
    vb = _tlv(0x30, _tlv(0x30, _oid(oid) + b"\x05\x00"))
    pdu = _tlv(tag, _int(rid) + _int(0) + _int(0) + vb)
    return _tlv(0x30, _int(ver) + _tlv(4, comm.encode()) + pdu)

def _parse(data, rid):
    _, c, _ = _read(data, 0)
    ch = _children(c)
    f = _children(ch[2][1])
    if _val(*f[0]) != rid:
        raise ValueError("id diferente")
    if _val(*f[1]):  # erro (ex.: noSuchName no SNMPv1 = fim da árvore)
        return None
    _, vb = _children(f[3][1])[0]
    nome, valor = _children(vb)
    return _oid_str(nome[1]), _val(*valor)

class Snmp:
    def __init__(self, ip, comunidade="public", porta=161, timeout=2.0, versao=1, tentativas=2):
        self.ip, self.com, self.porta = ip, comunidade, porta
        self.timeout, self.ver, self.tent = timeout, versao, tentativas  # versao: 0=v1, 1=v2c

    def _ask(self, tag, oid):
        for _ in range(self.tent):
            rid = random.randint(1, 2**30)
            msg = _request(self.ver, self.com, tag, rid, oid)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            try:
                sock.sendto(msg, (self.ip, self.porta))
                while True:
                    data, _ = sock.recvfrom(65535)
                    try:
                        return _parse(data, rid)
                    except ValueError:
                        continue
            except socket.timeout:
                continue
            except OSError:
                continue
            finally:
                sock.close()
        raise TimeoutError("sem resposta SNMP")

    def get(self, oid):
        r = self._ask(0xA0, oid)
        return None if r is None or r[1] is END else r[1]

    def walk(self, base):
        atual, res = base, []
        for _ in range(800):
            r = self._ask(0xA1, atual)
            if r is None:
                break
            oid, val = r
            if val is END or not oid.startswith(base + ".") or oid == atual:
                break
            res.append((oid, val))
            atual = oid
        return res

# ----------------------------- Leitura da impressora -----------------------------
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_NOME = "1.3.6.1.2.1.1.5.0"
HR_DESCR = "1.3.6.1.2.1.25.3.2.1.3.1"
IF_MAC = "1.3.6.1.2.1.2.2.1.6"
SERIE = "1.3.6.1.2.1.43.5.1.1.17.1"
PAGINAS = "1.3.6.1.2.1.43.10.2.1.4.1.1"
STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"
ERROS = "1.3.6.1.2.1.25.3.5.1.2.1"
SUPRIMENTOS = "1.3.6.1.2.1.43.11.1.1"

STATUS_TXT = {1: "Outro", 2: "Desconhecido", 3: "Pronta", 4: "Imprimindo", 5: "Aquecendo"}
ALERTAS = [(0x80, "Pouco papel"), (0x40, "Sem papel"), (0x20, "Pouco toner"), (0x10, "Sem toner"),
           (0x08, "Porta aberta"), (0x04, "Papel atolado"), (0x02, "Offline"), (0x01, "Requer manutenção")]

def _txt(b):
    if not isinstance(b, (bytes, bytearray)):
        return ""
    try:
        return b.decode("utf-8").strip("\x00 ")
    except UnicodeDecodeError:
        return b.decode("latin-1").strip("\x00 ")

def _categoria(tipo, desc):
    d = desc.lower()
    if tipo in (4, 8, 14, 24, 26) or any(w in d for w in ("waste", "resíduo", "residuo")):
        return "residuo"
    if tipo == 9 or any(w in d for w in ("drum", "cilindro", "imag", "photocond", "fotocond", "opc")):
        return "cilindro"
    if tipo in (3, 5, 6, 21) or any(w in d for w in ("toner", "cartridge", "cartucho", "ink", "tinta",
                                                       "black", "cyan", "magenta", "yellow", "preto")):
        return "toner"
    return "outro"

def ler_suprimentos(s):
    colunas = {}
    for oid, val in s.walk(SUPRIMENTOS):
        p = oid[len(SUPRIMENTOS) + 1:].split(".")
        colunas.setdefault(".".join(p[1:]), {})[int(p[0])] = val
    itens = []
    for c in colunas.values():
        desc, mx, lv = _txt(c.get(6)), c.get(8), c.get(9)
        pct = None
        if isinstance(lv, int) and isinstance(mx, int) and lv >= 0 and mx > 0:
            pct = max(0, min(100, round(lv * 100 / mx)))
        itens.append({"nome": desc, "tipo": _categoria(c.get(5), desc), "pct": pct, "nivel": lv, "maximo": mx})
    return itens

def menor(itens, tipo):
    v = [i["pct"] for i in itens if i["tipo"] == tipo and i["pct"] is not None]
    return min(v) if v else None

def coletar(ip, a):
    r = {"ip": ip, "online": False, "lido_em": datetime.now().isoformat(timespec="seconds")}
    s = descr = None
    for ver in ((0,) if a.v1 else (1, 0)):  # tenta v2c e, se não houver resposta, v1 (ex.: OKI em modo v3+v1)
        try:
            s = Snmp(ip, a.comunidade, a.porta, a.timeout, ver)
            descr = s.get(SYS_DESCR)
            r["snmp"] = "v1" if ver == 0 else "v2c"
            break
        except TimeoutError:
            s = None
    if s is None:
        r["erro"] = "Sem resposta SNMP (offline, SNMP desativado ou comunidade incorreta)"
        return r
    try:
        r["online"] = True
        r["descricao"] = _txt(descr)
        r["nome"] = _txt(s.get(SYS_NOME))
        r["modelo"] = _txt(s.get(HR_DESCR)) or _txt(descr)[:60]
        for _, v in s.walk(IF_MAC):
            if isinstance(v, bytes) and len(v) == 6 and any(v):
                r["mac"] = ":".join(f"{b:02X}" for b in v)
                break
        st = s.get(STATUS)
        r["status"] = STATUS_TXT.get(st, "—") if isinstance(st, int) else "—"
        er = s.get(ERROS)
        r["alertas"] = [n for bit, n in ALERTAS if isinstance(er, bytes) and er and er[0] & bit]
        r["serie"] = _txt(s.get(SERIE))
        pg = s.get(PAGINAS)
        r["paginas"] = pg if isinstance(pg, int) and pg >= 0 else None
        itens = ler_suprimentos(s)
        r["suprimentos"] = [i for i in itens if i["tipo"] != "outro"]
        r["toner_pct"] = menor(itens, "toner")
        r["cilindro_pct"] = menor(itens, "cilindro")
    except Exception as e:  # leitura parcial: mantém o que já foi coletado
        r["erro"] = f"Leitura parcial: {e}"
    return r

# ----------------------------- Lista de IPs -----------------------------
IPV4 = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$")

def _ip_de(v):
    if isinstance(v, (int, float)) and len(str(int(v))) == 12:  # 192168204181 -> 192.168.204.181
        s = str(int(v))
        return f"{s[:3]}.{s[3:6]}.{s[6:9]}.{s[9:]}"
    if isinstance(v, str):
        m = IPV4.match(v)
        return m.group(1) if m else None
    return None

def ler_ips(caminho):
    ips = []
    ext = os.path.splitext(caminho)[1].lower()
    if ext == ".json":
        d = json.load(open(caminho, encoding="utf-8"))
        lista = d.get("impressoras", []) if isinstance(d, dict) else d
        ips = [_ip_de(x.get("ip")) for x in lista]
    elif ext in (".xlsx", ".xlsm"):
        import openpyxl
        ws = openpyxl.load_workbook(caminho, data_only=True).worksheets[0]
        ips = [_ip_de(c) for row in ws.iter_rows(values_only=True) for c in row]
    else:
        for linha in open(caminho, encoding="utf-8-sig"):
            m = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", linha.split("#")[0])
            ips.append(m.group(0) if m else None)
    vistos = []
    for ip in ips:
        if ip and ip not in vistos:
            vistos.append(ip)
    return vistos

def rodar(a):
    ips = ler_ips(a.entrada)
    if not ips:
        sys.exit(f"Nenhum IP encontrado em {a.entrada}")
    print(f"Consultando {len(ips)} impressoras...")
    resultados, feitos = [], 0
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        for r in ex.map(lambda ip: coletar(ip, a), ips):
            feitos += 1
            t, c = r.get("toner_pct"), r.get("cilindro_pct")
            estado = "online " if r["online"] else "OFFLINE"
            print(f"[{feitos:>3}/{len(ips)}] {r['ip']:<16} {estado} toner={t if t is not None else '-'}% "
                  f"cilindro={c if c is not None else '-'}% {r.get('erro', '')}")
            resultados.append(r)
    saida = {"gerado_em": datetime.now().isoformat(timespec="seconds"), "impressoras": resultados}
    tmp = a.saida + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)
    os.replace(tmp, a.saida)
    on = sum(r["online"] for r in resultados)
    print(f"\nPronto: {on} online, {len(resultados) - on} offline. Arquivo: {a.saida}")

def main():
    p = argparse.ArgumentParser(description="Lê toner/cilindro das impressoras via SNMP")
    p.add_argument("-e", "--entrada", default="ips.txt", help="ips.txt, .csv, .json (backup do sistema) ou .xlsx")
    p.add_argument("-s", "--saida", default="leitura.json")
    p.add_argument("-c", "--comunidade", default="public")
    p.add_argument("--v1", action="store_true", help="usar SNMP v1 em vez de v2c")
    p.add_argument("--porta", type=int, default=161)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threads", type=int, default=20)
    p.add_argument("--a-cada", type=int, default=0, metavar="MIN", help="repetir a cada N minutos")
    a = p.parse_args()
    while True:
        rodar(a)
        if not a.a_cada:
            break
        print(f"Próxima leitura em {a.a_cada} min (Ctrl+C para parar)\n")
        time.sleep(a.a_cada * 60)

if __name__ == "__main__":
    main()
