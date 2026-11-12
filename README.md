# MikroKing — Painel de Monitoramento MikroTik

Painel web em tempo real para acompanhar o desempenho de um router MikroTik,
com foco na qualidade dos links de Internet, latência, tráfego e VLANs.

A conexão é feita pela **API nativa do RouterOS** (porta `8728` ou `8729` com
API-SSL), e **não** pela REST API sobre HTTP. Assim o painel não depende do
serviço `www`/`www-ssl` do router e mantém uma única conexão persistente,
reduzindo a carga sobre o equipamento.

---

## Funcionalidades

### Sistema
- Uso de CPU (%)
- Memória usada/total e percentual
- Uptime, versão do RouterOS, modelo/arquitetura
- Temperatura (quando o hardware suporta `/system/health`)
- Número de conexões ativas no firewall
- Número de leases DHCP

### Links de Internet (WAN)
- Detecção automática dos links pelas rotas default ativas
  (ou definição manual via `WAN_INTERFACES`)
- Status do link (up/down)
- Endereço IP e gateway
- Taxa negociada do link (ex.: `1Gbps`) para interfaces Ethernet
- Latência média (ping forçado pela interface do link)
- Perda de pacotes (%)
- Tráfego RX/TX atual em Mbps

### Gráficos em tempo real
- Latência por link (ms)
- Tráfego WAN RX/TX (Mbps)
- CPU e memória (%)

### VLANs e interfaces
- Tabela de VLANs com ID, interface pai e status
- Tabela completa de interfaces com tipo, status, RX/TX, MTU e MAC

### Outros
- Atualização automática a cada `REFRESH_INTERVAL` segundos
- Indicação visual de latência boa/atenção/crítica
- Histórico em memória configurável (`MAX_DATA_POINTS`)

---

## Requisitos

- Python 3.10+
- Router MikroTik com RouterOS v6.43+ (recomendado v7)
- Serviço **API** ou **API-SSL** habilitado no MikroTik
- Usuário com permissão de leitura (`read`, `api`, `rest-api`, `sensitive`)

---

## Configuração no MikroTik

### 1. Habilitar o serviço API

Via terminal do RouterOS:

```mikrotik
# API nativa sem criptografia (porta 8728)
/ip service enable api

# ou API-SSL (porta 8729) - recomendado
/ip service enable api-ssl
```

> Por segurança, prefira **API-SSL** e restrinja o acesso por endereço IP.

### 2. Criar um usuário somente leitura

```mikrotik
/user group add name=dashboard policy=read,api,rest-api,sensitive
/user add name=dashboard group=dashboard password="UMA_SENHA_FORTE"
```

Opcionalmente restrinja o IP de origem do usuário:

```mikrotik
/user set dashboard address=192.168.1.50/32
```

### 3. (Opcional) Restringir o serviço API

```mikrotik
/ip service set api address=192.168.1.0/24
/ip service set api-ssl address=192.168.1.0/24
```

---

## Instalação

```bash
# Clone o projeto ou copie os arquivos
cd MikroKing

# (Opcional, mas recomendado) crie um ambiente virtual
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# Instale as dependências
pip install -r requirements.txt
```

---

## Configuração do painel

1. Copie o arquivo de exemplo:

```bash
cp .env.example .env
```

2. Edite o `.env`:

```env
MIKROTIK_HOST=192.168.88.1
MIKROTIK_USER=dashboard
MIKROTIK_PASS=UMA_SENHA_FORTE
MIKROTIK_USE_SSL=false
MIKROTIK_API_PORT=8728
```

3. Ajuste os links WAN, se necessário:

```env
# Deixe vazio para detecção automática pelas rotas default
WAN_INTERFACES=

# Ou force os nomes das interfaces
# WAN_INTERFACES=ether1,pppoe-out1
WAN_LABELS=Link 1,Link 2
```

> Se as interfaces WAN forem PPPoE e a detecção automática não funcionar,
> informe os nomes em `WAN_INTERFACES`.

---

## Executar

```bash
python app.py
```

Acesse no navegador:

```
http://localhost:5000
```

Para expor na rede local, o servidor já escuta em `0.0.0.0:5000`.

---

## Estrutura do projeto

```
MikroKing/
├── app.py              # Backend Flask + coletor da API nativa
├── config.py           # Configurações (lê variáveis de ambiente / .env)
├── requirements.txt    # Dependências
├── .env.example        # Exemplo de configuração
├── templates/
│   └── base.html       # Painel web (Bootstrap + Chart.js)
└── README.md
```

---

## Como funciona

1. Uma thread em segundo plano abre uma conexão persistente com a API nativa
   do RouterOS usando `librouteros`.
2. A cada `REFRESH_INTERVAL` segundos ela coleta:
   - `/system/resource/print`
   - `/system/health/print` (quando disponível)
   - `/interface/print`
   - `/interface/vlan/print`
   - `/ip/route/print`
   - `/ip/address/print`
   - `/ping` forçando `interface=<wan>` para cada link
   - `/interface/ethernet/monitor` para a taxa negociada dos links Ethernet
   - Contadores de conexões e leases DHCP
3. Os dados são mantidos em memória com histórico limitado.
4. O frontend consulta `/api/metrics` e atualiza tabelas e gráficos.

---

## Variáveis de configuração

| Variável | Padrão | Descrição |
|---|---|---|
| `MIKROTIK_HOST` | `192.168.88.1` | IP/hostname do router |
| `MIKROTIK_USER` | `admin` | Usuário da API |
| `MIKROTIK_PASS` | *(vazio)* | Senha da API |
| `MIKROTIK_USE_SSL` | `false` | `true` para API-SSL (8729) |
| `MIKROTIK_API_PORT` | `8728`/`8729` | Porta da API |
| `MIKROTIK_API_TIMEOUT` | `10` | Timeout da conexão (s) |
| `WAN_INTERFACES` | *(auto)* | Interfaces WAN, separadas por vírgula |
| `WAN_LABELS` | `Link 1,Link 2` | Nomes amigáveis dos links |
| `WAN_PING_MODE` | `gateway` | `gateway` = pinga o next-hop de cada link; `public` = pinga `WAN_PING_TARGET` |
| `WAN_PING_TARGET` | `8.8.8.8` | Alvo de latência quando `WAN_PING_MODE=public` |
| `REFRESH_INTERVAL` | `5` | Intervalo de coleta (s) |
| `MAX_DATA_POINTS` | `60` | Pontos mantidos nos gráficos |
| `LATENCY_WARNING_MS` | `80` | Limite de atenção (ms) |
| `LATENCY_CRITICAL_MS` | `150` | Limite crítico (ms) |

---

## Solução de problemas

### `Connection refused` ou timeout
- Verifique se o serviço API está habilitado:
  ```mikrotik
  /ip service print
  ```
- Confirme IP, porta e usuário/senha no `.env`.
- Verifique firewall do MikroTik e da máquina onde o painel roda.

### Links WAN não aparecem
- Defina manualmente:
  ```env
  WAN_INTERFACES=ether1,pppoe-out1
  ```
- Verifique se existem rotas default ativas:
  ```mikrotik
  /ip route print where dst-address=0.0.0.0/0
  ```

### Latência sempre `--`
- O alvo `WAN_PING_TARGET` pode estar bloqueado pelo provedor.
- Teste manualmente:
  ```mikrotik
  /ping 8.8.8.8 interface=ether1 count=4
  ```

### Failover / link de backup (ex.: ether2)

Em configurações de failover, a rota default do link de backup fica **inativa**
enquanto o link principal está saudável. O painel considera **todas** as rotas
default válidas (ativas e inativas) na detecção, então o link de backup também
aparece.

Por padrão o painel pinga o **gateway (next-hop)** de cada link, pois ele está
diretamente conectado e responde mesmo quando o link não está roteando tráfego:

```env
WAN_PING_MODE=gateway
```

Se preferir medir a latência real de internet, use o modo público:

```env
WAN_PING_MODE=public
WAN_PING_TARGET=8.8.8.8
```

Nesse modo o painel detecta automaticamente a VRF/routing-table de cada link
(ex.: `vrf_brd`, `vrf_claro`) e executa o ping por ela, garantindo que o teste
saia pelo link correto mesmo em cenários de failover.

> Se o ping público falhar em algum link, mantenha `WAN_PING_MODE=gateway` para
> medir pelo menos o último trecho (gateway do link).

Para forçar exatamente os links que deseja monitorar:

```env
WAN_INTERFACES=ether3,ether2
WAN_LABELS=Link Principal,Link Backup
```

### Temperatura `--`
- Nem todo hardware expõe `/system/health`. Nesse caso o campo fica vazio.

---

## Segurança

- Use **API-SSL** em produção.
- Crie um usuário exclusivo com permissões mínimas (`read`, `api`, `rest-api`, `sensitive`).
- Restrinja o serviço API por endereço de origem.
- Não exponha a porta `5000` diretamente na Internet. Use um proxy reverso
  (Nginx/Traefik) com HTTPS e autenticação.
- O painel não armazena a senha em disco além do `.env` (que deve ter permissões restritas).

---

## Próximos passos sugeridos

- Persistência histórica com SQLite/InfluxDB/Prometheus
- Alertas por e-mail/Telegram quando um link cair ou a latência ultrapassar limites
- Gráficos por VLAN e por interface individual
- Exportação de métricas em `/metrics` para Prometheus
- Autenticação de usuários no painel
