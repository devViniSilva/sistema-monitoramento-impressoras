# Controle de Impressoras — React + Python

Adaptação do painel original para **React, JavaScript e CSS**, mantendo o servidor Python, a coleta SNMP e o formato dos dados existentes.

## O que foi preservado

- Cadastro, edição e exclusão de impressoras (perfil administrador).
- Consulta para usuários com perfil somente leitura.
- Busca por setor, unidade, marca, modelo, IP, MAC e número de série.
- Filtros de unidade, SEDE, setor, offline, scanner com problema e níveis baixos.
- Status online/offline, alertas SNMP, contador de páginas e data da leitura.
- Níveis de toner/tinta e cilindro, cores, origem da leitura (SNMP/NDD), previsão e data.
- Importação do backup JSON, do `leitura.json` do coletor e do CSV do NDD.
- Exportação de backup JSON completo.
- Relatório de suprimentos para copiar ou abrir no WhatsApp.
- Coleta SNMP manual e automática, descoberta de impressoras por faixa de rede.
- Login, sessão, bloqueio após tentativas incorretas, perfis e proteção das rotas de escrita.
- Atualização periódica da tela a cada 15 segundos.
- Tema claro e escuro com preferência guardada no navegador.

## Estrutura

```text
controle-impressoras-react/
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── api.js
│   │   ├── main.jsx
│   │   └── styles.css
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── servidor_impressoras.py
├── coletor_impressoras.py
├── relatorio.py
├── dados.json
├── usuarios.json
└── impressoras-legado.html
```

## 1. Instalar o Node.js

Instale uma versão LTS atual do Node.js. Depois abra o terminal na pasta `frontend` e execute:

```bash
npm install
npm run build
```

Isso cria `frontend/dist`, que é a versão React servida pelo Python.

## 2. Iniciar o servidor Python

Volte para a pasta principal `controle-impressoras-react` e execute o comando que você já utiliza, por exemplo:

```bash
python servidor_impressoras.py --faixas 192.168.204.0/24
```

O painel ficará disponível em `http://IP-DO-SERVIDOR:8080` (ou na porta que você configurar). Mantenha os parâmetros de rede, SNMP, HTTPS e proxy que já utiliza no ambiente atual.

O servidor continua lendo e gravando `dados.json` e `usuarios.json` na pasta principal. **Não substitua esses arquivos pelos originais antigos depois de começar a usar a versão nova**, pois são a base dos registros atuais.

## 3. Modo desenvolvimento

Terminal 1, na pasta principal:

```bash
python servidor_impressoras.py --faixas 192.168.204.0/24
```

Terminal 2, na pasta `frontend`:

```bash
npm install
npm run dev
```

Acesse `http://localhost:5173`. O Vite encaminha as chamadas `/api` para o servidor Python em `127.0.0.1:8080`.

## Segurança e dados

- `dados.json` contém inventário e endereços internos de rede; `usuarios.json` contém hashes de senha e token de API. Não publique esses arquivos em repositórios públicos nem os envie a terceiros.
- Faça cópia de segurança de `dados.json` e `usuarios.json` antes de atualizar o servidor.
- Continue usando HTTPS quando o sistema for acessado pela rede, conforme a configuração já existente.
- O frontend não recebe a senha da sessão: o cookie continua `HttpOnly`, controlado pelo Python.

## Observação sobre compatibilidade

A interface React usa as rotas atuais (`/api/me`, `/api/login`, `/api/logout`, `/api/printers`, `/api/coletar`). O formato dos objetos de impressora não foi remodelado: campos de inventário, `leitura` e `ndd` são mantidos no JSON. O arquivo `impressoras-legado.html` foi guardado como referência, sem ser utilizado pela nova interface.
