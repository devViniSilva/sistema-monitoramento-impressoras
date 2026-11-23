#!/usr/bin/env python3
"""
Servidor do Controle de Impressoras para a equipe (só precisa do Python).

Arquivos na mesma pasta: servidor_impressoras.py, coletor_impressoras.py, relatorio.py, impressoras.html

1) Criar os usuários (uma vez):
    python servidor_impressoras.py --criar-usuario vinicius --perfil admin
    python servidor_impressoras.py --criar-usuario maria --perfil leitura
   (perfil admin: importa, edita, exclui e coleta | perfil leitura: só consulta)
2) Iniciar:
    python servidor_impressoras.py --faixas 192.168.204.0/24,10.8.1.0/24

Gerenciar usuários:  --listar-usuarios | --remover-usuario NOME | --criar-usuario NOME (de novo = troca a senha)
Token para o robô/relatório:  --gerar-token-api   (coloque no config.json do robô)

Outras opções:
    --porta 8080  --dados dados.json  --usuarios usuarios.json  --sessao-horas 10
    --coletar-a-cada 30  --comunidade public  --v1  --faixas ...  --descobrir-a-cada 24
    --certificado cert.pem --chave chave.pem      liga HTTPS (recomendado)
    --senha-edicao SENHA                          modo antigo, só use se NÃO houver usuários
"""
import argparse, base64, getpass, hashlib, hmac, ipaddress, json, mimetypes, os, secrets, socket, ssl, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs

import coletor_impressoras as C
import relatorio as R

AQUI = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
SLOCK = threading.Lock()
ACORDAR = threading.Event()
ESTADO = {"coletando": False}
SESSOES = {}   # token -> {usuario, perfil, exp}
FALHAS = {}    # ip -> [contagem, bloqueado_ate]
ITER = 200_000
MAX_FALHAS, BLOQUEIO_S = 5, 300


def agora():
    return datetime.now().isoformat(timespec="seconds")


def b64(b):
    return base64.b64encode(b).decode()


# ----------------------------- dados e usuários -----------------------------
def _carregar(arq, padrao):
    if os.path.exists(arq):
        try:
            with open(arq, encoding="utf-8") as f:
                padrao.update(json.load(f))
        except Exception as e:
            raise SystemExit(f"Não consegui ler {arq}: {e}")
    return padrao


def _salvar(arq, d):
    tmp = arq + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, arq)


class Dados:
    def __init__(self, arq):
        self.arq = arq
        self.d = _carregar(arq, {"seeded": False, "printers": {}, "coleta": ""})

    def salvar(self):
        _salvar(self.arq, self.d)


class Usuarios:
    def __init__(self, arq):
        self.arq = arq
        self.d = _carregar(arq, {"usuarios": {}, "token_api": ""})

    def salvar(self):
        _salvar(self.arq, self.d)

    @staticmethod
    def _hash(senha, salt, it=ITER):
        return hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, it)

    def criar(self, nome, senha, perfil):
        salt = os.urandom(16)
        self.d["usuarios"][nome.lower()] = {
            "hash": f"pbkdf2_sha256${ITER}${b64(salt)}${b64(self._hash(senha, salt))}", "perfil": perfil}
        self.salvar()

    def verificar(self, nome, senha):
        rec = self.d["usuarios"].get((nome or "").strip().lower())
        try:
            _, it, salt, h = (rec or {"hash": "x$1$AA==$AA=="})["hash"].split("$")
            calc = self._hash(senha or "", base64.b64decode(salt), int(it))
            ok = hmac.compare_digest(calc, base64.b64decode(h))
        except Exception:
            ok = False
        return rec["perfil"] if rec and ok else None

    @property
    def ativo(self):
        return bool(self.d["usuarios"])


# ----------------------------- coleta automática -----------------------------
def ciclo_coleta(dados, cfg):
    with LOCK:
        ips = sorted({(p.get("ip") or "").strip() for p in dados.d["printers"].values() if p.get("ip")})
    with ThreadPoolExecutor(cfg.threads) as ex:
        res = {r["ip"]: r for r in ex.map(lambda ip: C.coletar(ip, cfg), ips)}
    with LOCK:
        for p in dados.d["printers"].values():
            r = res.get((p.get("ip") or "").strip())
            if not r:
                continue
            p["leitura"] = r
            if r.get("online"):
                for campo in ("serie", "mac", "modelo"):
                    if not p.get(campo) and r.get(campo):
                        p[campo] = r[campo]
        dados.d["coleta"] = agora()
        dados.salvar()
    on = sum(r["online"] for r in res.values())
    print(f"[{agora()}] Coleta: {on} online, {len(res) - on} sem resposta de {len(res)}")


def sonda(ip, cfg):
    for ver in ((0,) if cfg.v1 else (1, 0)):  # v2c e depois v1
        s = C.Snmp(ip, cfg.comunidade, cfg.porta, 1.0, ver, 1)
        try:
            s.get(C.SYS_DESCR)
        except Exception:
            continue
        try:  # só aceita equipamentos que respondem como impressora
            if s.get(C.STATUS) is not None or s.get(C.SERIE) is not None:
                return ip
        except Exception:
            pass
        return None
    return None


def descobrir(dados, cfg):
    with LOCK:
        conhecidos = {(p.get("ip") or "").strip() for p in dados.d["printers"].values()}
    novos = []
    for faixa in cfg.faixas:
        hosts = [str(h) for h in ipaddress.ip_network(faixa, strict=False).hosts()]
        if len(hosts) > 4096:
            print(f"Faixa {faixa} muito grande, ignorada (máximo /20).")
            continue
        with ThreadPoolExecutor(64) as ex:
            novos += [ip for ip in ex.map(lambda i: sonda(i, cfg), hosts) if ip and ip not in conhecidos]
    for ip in novos:
        r = C.coletar(ip, cfg)
        with LOCK:
            dados.d["printers"]["d" + ip.replace(".", "_")] = {
                "id": "d" + ip.replace(".", "_"), "setor": "(novo) " + (r.get("nome") or ip), "local": "",
                "modelo": r.get("modelo", ""), "ip": ip, "mac": r.get("mac", ""), "serie": r.get("serie", ""),
                "scanner": "", "leitura": r}
            dados.d["seeded"] = True
            dados.salvar()
    print(f"[{agora()}] Descoberta: {len(novos)} impressoras novas")


def loop_coleta(dados, cfg):
    ultima_desc = 0.0
    while True:
        ESTADO["coletando"] = True
        try:
            if cfg.faixas and time.time() - ultima_desc >= cfg.descobrir_a_cada * 3600:
                descobrir(dados, cfg)
                ultima_desc = time.time()
            ciclo_coleta(dados, cfg)
        except Exception as e:
            print("Erro na coleta:", e)
        ESTADO["coletando"] = False
        ACORDAR.wait(cfg.coletar_a_cada * 60)
        ACORDAR.clear()


# ----------------------------- página de login -----------------------------
LOGIN_HTML = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Entrar – Controle de Impressoras</title>
<style>:root{--bg:#f5f6f8;--card:#fff;--tx:#1b1f27;--mut:#6b7280;--bd:#e3e6eb;--ac:#2563eb;--er:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#12141a;--card:#1b1f27;--tx:#e8eaee;--mut:#9aa3b2;--bd:#2d333f;--ac:#60a5fa;--er:#f87171}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--tx);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}
.c{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:26px;width:min(360px,92vw)}
h1{font-size:20px;margin:0 0 4px}p{color:var(--mut);margin:0 0 18px}label{display:block;font-size:12px;color:var(--mut);margin:12px 0 4px}
input{width:100%;font:inherit;color:var(--tx);background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px}
button{width:100%;margin-top:18px;font:inherit;font-weight:600;color:#fff;background:var(--ac);border:0;border-radius:8px;padding:11px;cursor:pointer}
#e{color:var(--er);min-height:20px;margin-top:12px;font-size:13px}</style></head><body>
<form class="c" id="f"><h1>🖨️ Controle de Impressoras</h1><p>Entre com seu usuário e senha.</p>
<label for="u">Usuário</label><input id="u" autocomplete="username" autofocus required>
<label for="s">Senha</label><input id="s" type="password" autocomplete="current-password" required>
<button>Entrar</button><div id="e"></div></form>
<script>document.getElementById("f").onsubmit=async ev=>{ev.preventDefault();const e=document.getElementById("e");e.textContent="";
try{const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({usuario:document.getElementById("u").value,senha:document.getElementById("s").value})});
if(r.ok){location.href="/"}else{const j=await r.json().catch(()=>({}));e.textContent=j.erro||"Não foi possível entrar."}}catch(x){e.textContent="Sem conexão com o servidor."}};</script></body></html>"""


# ----------------------------- servidor web -----------------------------
def criar_handler(dados, users, cfg, pagina, auto, https):
    class H(BaseHTTPRequestHandler):
        timeout = 30  # derruba conexões lentas/penduradas

        def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

        # --- sessão ---
        def _token(self):
            for parte in self.headers.get("Cookie", "").split(";"):
                k, _, v = parte.strip().partition("=")
                if k == "sessao":
                    return v
            return ""

        def _sess(self):
            t = self._token()
            with SLOCK:
                s = SESSOES.get(t)
                if s and s["exp"] > time.time():
                    s["exp"] = time.time() + cfg.sessao_horas * 3600  # renova a cada uso
                    return s
                SESSOES.pop(t, None)
            return None

        def _ip(self):
            """IP real do usuário. Atrás do Apache, só confia no X-Forwarded-For se a conexão vier do próprio servidor."""
            ip = self.client_address[0]
            if cfg.atras_proxy and ip in ("127.0.0.1", "::1"):
                xff = self.headers.get("X-Forwarded-For", "")
                if xff:
                    return xff.split(",")[-1].strip()  # o último valor foi acrescentado pelo Apache
            return ip

        def _seguro(self):
            if https or cfg.cookie_seguro:
                return True
            return (cfg.atras_proxy and self.client_address[0] in ("127.0.0.1", "::1")
                    and self.headers.get("X-Forwarded-Proto", "").lower() == "https")

        def _cookie(self, valor, max_age):
            return ("Set-Cookie", f"sessao={valor}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}" + ("; Secure" if self._seguro() else ""))

        def _origem_ok(self):
            o = self.headers.get("Origin")
            return not o or urlparse(o).netloc == self.headers.get("Host")

        def _ler(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(n, 1_000_000))

        # --- regras de acesso ---
        def _leitura(self, token_api=False):
            """True se pode ler; senão já responde 401."""
            if not users.ativo:
                return True
            if self._sess():
                return True
            tk = users.d.get("token_api") or ""
            if token_api and tk and hmac.compare_digest(self.headers.get("X-Token", ""), tk):
                return True
            self._json(401, {"erro": "login necessário"})
            return False

        def _escrita(self):
            if not self._origem_ok():
                self._json(403, {"erro": "origem não permitida"})
                return False
            if users.ativo:
                s = self._sess()
                if not s:
                    self._json(401, {"erro": "login necessário"})
                    return False
                if s["perfil"] != "admin":
                    self._json(403, {"erro": "seu usuário tem acesso somente de leitura"})
                    return False
                return True
            if cfg.senha_edicao and self.headers.get("X-Senha") != cfg.senha_edicao:
                self._json(401, {"erro": "senha de edição necessária"})
                return False
            return True

        # --- rotas ---
        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/impressoras.html", "/login"):
                # Frontend React compilado pelo Vite. A autenticação é feita pela própria tela React.
                index_react = os.path.join(AQUI, "frontend", "dist", "index.html")
                pagina_atual = index_react if os.path.isfile(index_react) else pagina
                try:
                    with open(pagina_atual, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    self._json(404, {"erro": "Frontend não encontrado. Execute npm run build na pasta frontend."})
            elif p.startswith("/assets/") or p == "/favicon.svg":
                dist = os.path.realpath(os.path.join(AQUI, "frontend", "dist"))
                alvo = os.path.realpath(os.path.join(dist, p.lstrip("/")))
                if not alvo.startswith(dist + os.sep) or not os.path.isfile(alvo):
                    return self._send(404)
                tipo = mimetypes.guess_type(alvo)[0] or "application/octet-stream"
                with open(alvo, "rb") as f:
                    self._send(200, f.read(), tipo)
            elif p == "/api/me":
                if not users.ativo:
                    return self._json(200, {"usuario": None, "perfil": "admin", "auth": False})
                s = self._sess()
                if not s:
                    return self._json(401, {"erro": "login necessário"})
                self._json(200, {"usuario": s["usuario"], "perfil": s["perfil"], "auth": True})
            elif p == "/api/printers":
                if not self._leitura():
                    return
                with LOCK:
                    self._json(200, {"seeded": dados.d["seeded"], "printers": list(dados.d["printers"].values()),
                                     "coleta": dados.d.get("coleta", ""), "auto": auto, "coletando": ESTADO["coletando"]})
            elif p == "/api/relatorio":  # ?nivel=20&unidade=SEDE&setor=...&q=...
                if not self._leitura(token_api=True):
                    return
                qs = parse_qs(urlparse(self.path).query)
                g = lambda k: (qs.get(k) or [""])[0].strip()
                with LOCK:
                    todas = list(dados.d["printers"].values())
                texto = R.relatorio(todas, int(g("nivel")) if g("nivel").isdigit() else None, g("unidade"), g("setor"), g("q"))
                self._send(200, texto.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self._send(404)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/login":
                if not self._origem_ok():
                    return self._json(403, {"erro": "origem não permitida"})
                ip = self._ip()
                with SLOCK:
                    cont, ate = FALHAS.get(ip, [0, 0])
                    if ate > time.time():
                        return self._json(429, {"erro": f"Muitas tentativas. Aguarde {int(ate - time.time()) // 60 + 1} min."})
                try:
                    b = json.loads(self._ler() or b"{}")
                    perfil = users.verificar(str(b.get("usuario", "")), str(b.get("senha", "")))
                except Exception:
                    perfil = None
                if not perfil:
                    with SLOCK:
                        cont += 1
                        FALHAS[ip] = [cont, time.time() + BLOQUEIO_S if cont >= MAX_FALHAS else 0]
                        if cont >= MAX_FALHAS:
                            FALHAS[ip][0] = 0
                    print(f"[{agora()}] Login recusado ({ip})")
                    time.sleep(0.5)
                    return self._json(401, {"erro": "Usuário ou senha incorretos."})
                with SLOCK:
                    FALHAS.pop(ip, None)
                    tk = secrets.token_urlsafe(32)
                    nome = str(b.get("usuario")).strip().lower()
                    SESSOES[tk] = {"usuario": nome, "perfil": perfil, "exp": time.time() + cfg.sessao_horas * 3600}
                print(f"[{agora()}] Login: {nome} ({perfil}) de {ip}")
                return self._json(200, {"ok": True, "perfil": perfil}, extra=[self._cookie(tk, cfg.sessao_horas * 3600)])
            if p == "/api/logout":
                with SLOCK:
                    SESSOES.pop(self._token(), None)
                return self._json(200, {"ok": True}, extra=[self._cookie("", 0)])
            if p == "/api/coletar":
                if not self._escrita():
                    return
                if not auto:
                    return self._json(400, {"erro": "coleta automática desligada"})
                ACORDAR.set()
                return self._json(200, {"ok": True})
            self._send(404)

        def _id(self):
            m = urlparse(self.path).path.split("/")
            return unquote(m[3]) if len(m) == 4 and m[1:3] == ["api", "printers"] and m[3] else None

        def do_PUT(self):
            pid = self._id()
            if not pid:
                return self._send(404)
            if not self._escrita():
                return
            try:
                obj = json.loads(self._ler())
                assert isinstance(obj, dict)
            except Exception:
                return self._json(400, {"erro": "JSON inválido"})
            obj["id"] = pid
            with LOCK:
                dados.d["printers"][pid] = obj
                dados.d["seeded"] = True
                dados.salvar()
            self._json(200, {"ok": True})

        def do_DELETE(self):
            pid = self._id()
            if not pid:
                return self._send(404)
            if not self._escrita():
                return
            with LOCK:
                dados.d["printers"].pop(pid, None)
                dados.d["seeded"] = True
                dados.salvar()
            self._json(200, {"ok": True})

        def log_message(self, *a):
            pass

    return H


def meu_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # não envia nada; só descobre a placa de rede em uso
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def pedir_senha(nome):
    s1 = getpass.getpass(f"Senha para {nome} (mín. 8 caracteres): ")
    if len(s1) < 8:
        raise SystemExit("Senha muito curta.")
    if s1 != getpass.getpass("Repita a senha: "):
        raise SystemExit("As senhas não conferem.")
    return s1


def main():
    a = argparse.ArgumentParser(description="Servidor do Controle de Impressoras")
    a.add_argument("--host", default="0.0.0.0", help="endereço de escuta (use 127.0.0.1 atrás do Apache)")
    a.add_argument("--porta", type=int, default=8080)
    a.add_argument("--atras-de-proxy", action="store_true", help="confia no X-Forwarded-* enviados pelo Apache local")
    a.add_argument("--cookie-seguro", action="store_true", help="marca o cookie como Secure (use com HTTPS no Apache)")
    a.add_argument("--sem-login", action="store_true", help="permite subir sem usuários (NÃO recomendado)")
    a.add_argument("--dados", default=os.path.join(AQUI, "dados.json"))
    a.add_argument("--usuarios", default=os.path.join(AQUI, "usuarios.json"))
    a.add_argument("--criar-usuario", metavar="NOME")
    a.add_argument("--perfil", choices=["admin", "leitura"], default="leitura")
    a.add_argument("--remover-usuario", metavar="NOME")
    a.add_argument("--listar-usuarios", action="store_true")
    a.add_argument("--gerar-token-api", action="store_true")
    a.add_argument("--sessao-horas", type=float, default=10)
    a.add_argument("--certificado", default="")
    a.add_argument("--chave", default="")
    a.add_argument("--senha-edicao", default="")
    a.add_argument("--coletar-a-cada", type=int, default=30, metavar="MIN")
    a.add_argument("--comunidade", default="public")
    a.add_argument("--v1", action="store_true")
    a.add_argument("--faixas", default="", help="ex.: 192.168.204.0/24,10.8.1.0/24")
    a.add_argument("--descobrir-a-cada", type=float, default=24, metavar="HORAS")
    a.add_argument("--snmp-porta", dest="porta_snmp", type=int, default=161)
    a.add_argument("--timeout", type=float, default=2.0)
    a.add_argument("--threads", type=int, default=20)
    a = a.parse_args()

    users = Usuarios(a.usuarios)
    if a.criar_usuario:
        users.criar(a.criar_usuario, pedir_senha(a.criar_usuario), a.perfil)
        raise SystemExit(f"Usuário '{a.criar_usuario.lower()}' salvo com perfil {a.perfil}.")
    if a.remover_usuario:
        ok = users.d["usuarios"].pop(a.remover_usuario.lower(), None)
        users.salvar()
        raise SystemExit("Removido." if ok else "Usuário não encontrado.")
    if a.listar_usuarios:
        for n, u in users.d["usuarios"].items():
            print(f"- {n} ({u['perfil']})")
        raise SystemExit(0 if users.d["usuarios"] else "Nenhum usuário cadastrado.")
    if a.gerar_token_api:
        users.d["token_api"] = secrets.token_urlsafe(24)
        users.salvar()
        raise SystemExit("Token do robô/relatório (guarde em segredo):\n" + users.d["token_api"])

    if not users.ativo and not a.sem_login:
        raise SystemExit("Nenhum usuário cadastrado: por segurança o servidor não sobe sem login.\n"
                         "Crie um com: python servidor_impressoras.py --criar-usuario NOME --perfil admin")
    a.faixas = [f.strip() for f in a.faixas.split(",") if f.strip()]
    cfg = argparse.Namespace(comunidade=a.comunidade, v1=a.v1, porta=a.porta_snmp, timeout=a.timeout, threads=a.threads,
                             faixas=a.faixas, descobrir_a_cada=a.descobrir_a_cada, coletar_a_cada=a.coletar_a_cada,
                             sessao_horas=a.sessao_horas, senha_edicao=a.senha_edicao,
                             atras_proxy=a.atras_de_proxy, cookie_seguro=a.cookie_seguro)
    pagina = os.path.join(AQUI, "impressoras.html")
    if not os.path.exists(pagina):
        raise SystemExit("Coloque o impressoras.html na mesma pasta deste arquivo.")
    dados = Dados(a.dados)
    auto = a.coletar_a_cada > 0
    https = bool(a.certificado and a.chave)
    srv = ThreadingHTTPServer((a.host, a.porta), criar_handler(dados, users, cfg, pagina, auto, https))
    if https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(a.certificado, a.chave)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    esq = "https" if https else "http"
    if a.host in ("127.0.0.1", "localhost"):
        print(f"Servidor no ar SOMENTE em {a.host}:{a.porta} (acesso externo via Apache).")
    else:
        print(f"Servidor no ar. A equipe acessa:  {esq}://{meu_ip()}:{a.porta}")
    print(f"Neste computador:                  {esq}://localhost:{a.porta}")
    if users.ativo:
        print(f"Login obrigatório ({len(users.d['usuarios'])} usuários, sessão de {a.sessao_horas:g} h).")
        if not https:
            print("Aviso: sem HTTPS, as senhas trafegam sem criptografia na rede. Veja --certificado e --chave.")
    else:
        print("ATENÇÃO: nenhum usuário cadastrado, a página está ABERTA a quem acessar a rede.")
        print("         Crie usuários com:  python servidor_impressoras.py --criar-usuario NOME --perfil admin")
    print(f"Coleta automática a cada {a.coletar_a_cada} min." if auto else "Coleta automática desligada.")
    if a.faixas:
        print("Varrendo em busca de impressoras novas em:", ", ".join(a.faixas))
    print(f"Dados salvos em: {a.dados}\nCtrl+C para parar.")
    if auto:
        threading.Thread(target=loop_coleta, args=(dados, cfg), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
