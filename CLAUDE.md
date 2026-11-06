# CLAUDE.md — Klipper-IP-Status

Contexto para quem trabalha neste repositório (Claude incluído). Escrito em português europeu porque é como o autor trabalha comigo; o **código, o README, as mensagens do programa e os commits são em inglês** (ver secção 7). Versão descrita: **0.5.1**.

## 1. Objetivo

Macro de consola `SHOW_IP_ADDRESS` para máquinas Klipper (Mainsail, KlipperScreen no ecrã): mostra na consola o IP de cada interface física (Wi-Fi, cabo, USB), o hostname (e `.local` quando funciona), a ordem em que o sistema prefere as ligações (`Internet:`) e, opcionalmente, as VPN.

- **Para quem:** o autor e os clientes a quem entrega impressoras; quem recebe a máquina descobre o IP sem router, ecrã ou scanner de rede.
- **Porquê:** nas máquinas entram redes diferentes (Wi-Fi do cliente, cabo, hotspot de telemóvel/router USB) e é preciso saber por onde a máquina está acessível.
- **Regra de produto:** o IP **aparece sempre**. Tudo o resto (hostname, VPN, ordem de internet) são linhas extra que se podem desligar ou omitir sem tirar os IPs.
- Só lê, nunca altera a rede da máquina.

## 2. Ambiente

- Klipper + Moonraker + Mainsail (a consola onde se vê o resultado), KlipperScreen no ecrã. Layout por omissão: `~/printer_data`, `~/moonraker`.
- Raspberry Pi OS (Debian) com **NetworkManager**; na máquina de teste os perfis vêm do netplan (`netplan-eth0`, `netplan-wlan0-<ssid>`). Precisa de `iproute2` (`ip -j`), systemd e `[respond]` no Klipper (o `mainsail.cfg` já o tem).
- Python do Moonraker (o componente corre dentro dele; sem dependências externas).
- **Máquina de teste real:** família Raspberry Pi 3: Ethernet `eth0` (driver `smsc95xx`, ligado por USB internamente), hotspot USB `eth1` (driver `cdc_ether`), Wi-Fi `wlan0` numa pen USB (`mt7601u`), VPN (`tailscale0`).
- **Pi 5:** a classificação nunca foi confirmada lá. Não assumir o modelo da máquina: pedir `cat /proc/device-tree/model`.
- Relacionados (não alterar a partir daqui): `print-from-usb-klipper` (imprimir de pen USB; clone só de leitura) e o projeto `klipper-hotspot` (Pi em modo hotspot, repositório próprio, mesmo estilo).
- Repositório: `https://github.com/lrafa97/Klipper-IP-Status`, branch `main`. O endereço antigo em minúsculas redireciona; o aviso "This repository moved" no `git push` é inofensivo.

## 3. Arquitetura

| Ficheiro | Papel |
| --- | --- |
| `src/network_info.py` | Componente Moonraker `network_info`. Corre `ip -j address` e `ip -j route show default`, classifica as interfaces, monta as linhas e envia-as à consola com `RESPOND` num só script. |
| `src/network_info.cfg` | Macro Klipper `SHOW_IP_ADDRESS`: só chama `action_call_remote_method("network_info_show")`. O cabeçalho não pode conter o texto `network_info.cfg` (o instalador procura-o para detetar o include). |
| `common.sh` | Variáveis, validações e funções partilhadas por `install.sh` e `uninstall.sh`. |
| `install.sh` / `uninstall.sh` | Instalação e remoção idempotentes (secção 4 e 5). |
| `VERSION` | Versão; o componente regista-a no log do Moonraker ao arrancar. |
| `tests/test_network_info.py` | 55 testes do componente, com Moonraker e `/sys` falsos. |
| `tests/test_install.sh` | 65 verificações do instalador numa máquina falsa em pasta temporária. |
| `README.md` | Documentação de utilizador (inglês). |

Fluxo: macro → `action_call_remote_method` → componente → `ip` + `/sys/class/net` → `RESPOND` por linha.

Funções puras no componente (testadas sem Moonraker): `classify`, `is_vpn`, `parse_addresses`, `parse_default_routes`, `build_lines`, `build_internet_line`, `build_hostname_line`.

## 4. Instalar, atualizar e testar

**Instalar** (utilizador normal, sem `sudo`; pede `sudo` só para reiniciar serviços):
```
cd ~ && git clone https://github.com/lrafa97/Klipper-IP-Status.git && bash ./Klipper-IP-Status/install.sh
```
**Atualizar** (macro inalterada desde a v0.2.0, basta reiniciar o Moonraker):
```
cd ~/Klipper-IP-Status && git pull && sudo systemctl restart moonraker
```
ou Mainsail → Machine → Update Manager → *klipper-ip-status*. Para refrescar o bloco no `moonraker.conf`: `cd ~/Klipper-IP-Status && git pull && bash install.sh`. Se a macro mudar, reiniciar o Klipper com a impressora parada.
**Remover:** `bash ~/Klipper-IP-Status/uninstall.sh` (`--force` ignora o aviso de impressão em curso).

**Testes (não precisam de hardware):**
```
python3 -I -m unittest discover -s tests
bash tests/test_install.sh
```
Correr os dois antes de cada commit.

**Só se confirma em máquina real:** formato real de `ip -j address` e `ip -j route show default` (o segundo nunca foi capturado; o código tolera campos em falta), caminhos e drivers em `/sys` (`readlink -f /sys/class/net/<if>/device/driver`), avahi a correr, comportamento do NetworkManager com várias placas, arranque com hotspot ligado, e o `install.sh` com o `sudo`/reinício reais.

Comando de diagnóstico de interfaces (uma linha, pedir sempre este):
```
for i in /sys/class/net/*; do echo "$(basename $i) | $(cat $i/address 2>/dev/null) | $(basename "$(readlink -f $i/device/driver 2>/dev/null)") | $(readlink -f $i)"; done
```

## 5. Decisões técnicas

- **Componente Moonraker + macro fina.** Uma macro Klipper não lê a rede nem corre comandos; pede ao Moonraker via `action_call_remote_method`. O componente fica ligado (symlink) ao repositório clonado, por isso o Update Manager troca o código sem reinstalar.
- **Leitura fresca em cada chamada** (`ip -j address`, timeout de 5 s, mata o processo se expirar). Sem cache: um hotspot ligado há segundos já aparece.
- **Classificação por hardware, não por nome.** `eth0`/`eth1` são atribuídos por ordem de deteção. `/sys/class/net/<if>/device` existente = física; `type` 1 = Ethernet/Wi-Fi; pasta `wireless` = Wi-Fi.
- **USB = driver de tethering** (`cdc_ether`, `rndis_host`, `cdc_ncm`, `cdc_eem`, `ipheth`, `huawei_cdc_ncm`) numa interface sob `/usbN/`; qualquer outro driver Ethernet é **Cabo**, mesmo ligado por USB (a ficha RJ45 do Pi 3 e adaptadores USB-RJ45). Se o driver não puder ser lido, mantém-se USB. Lista de drivers = lista de permitidos (os de tethering são poucos e estáveis; os de adaptadores RJ45 são muitos). *Rejeitado:* decidir só pelo caminho USB (marcava o cabo do Pi 3 como USB) e decidir pelo nome.
- **Linha `Internet:` = ordem configurada** (rota por omissão com a métrica mais baixa primeiro; iguais juntas com `or`). Não testa se há internet de facto. Se as rotas não puderem ser lidas, só essa linha desaparece. VPN nunca entra nesta linha.
- **Mudar a prioridade de rede não faz parte do programa.** É configuração do NetworkManager por máquina, com risco de cortar o acesso; fica fora do repositório (ver secção 6 para o perfil do hotspot).
- **VPN opcional** (`show_vpn`, desligada por omissão): interfaces virtuais de tipo 65534 (Tailscale, WireGuard, OpenVPN tun), listadas por último.
- **Hostname** (`show_hostname`, ligada): `Hostname: NOME (NOME.local)`; o `.local` só aparece se o avahi estiver a correr (`/run/avahi-daemon/pid`). É linha extra; os IPs não dependem dela.
- **Textos seguros para `RESPOND`:** `_clean()` remove `" { } ; \`, quebras de linha. IPv6 e `169.254.x.x` ignorados. Interface "ligada" = operstate UP/UNKNOWN sem `NO-CARRIER`.
- **Todas as etiquetas e textos editáveis** na secção `[network_info]` do `moonraker.conf` (tabela no README).
- **Instalador conservador:** corre como utilizador normal (`ALLOW_ROOT=1` só para testes); verifica tudo antes de alterar (conflito de nome da macro, componente de outrem, `CONFIG_SUBDIR` inválido); guarda a primeira cópia de `printer.cfg`/`moonraker.conf` em `config/klipper-ip-status-backup`; bloco marcado `# >>> klipper-ip-status >>>` … `# <<< klipper-ip-status <<<` no `moonraker.conf` (secção `[network_info]` + entrada `[update_manager klipper-ip-status]`); `[include network/network_info.cfg]` na **linha 1** do `printer.cfg` (o fim pertence ao `SAVE_CONFIG`); uma `[network_info]` escrita à mão fora do bloco é respeitada; reinicia Moonraker+Klipper na primeira instalação, só o Moonraker nas atualizações, e nada durante uma impressão. Opções que o utilizador pôs dentro do bloco sobrevivem a nova execução (v0.3.1); `show_vpn: False` é escrito por omissão para ser fácil de encontrar.
- **Versão sobe a cada alteração** (`VERSION`), e o componente regista-a no log.

## 6. Não repetir

- **"USB (eth0)" no Pi 3.** A ficha de cabo do Pi 3 está ligada ao barramento USB interno; classificar pelo caminho marcava-a como USB. Resolvido com o driver (v0.4.0). Foi preciso o output do comando de diagnóstico: **não adivinhar o hardware, pedir o output primeiro.**
- **Hotspot em "getting an address" com o cabo ligado.** O perfil `netplan-eth0` do NetworkManager serve *qualquer* placa de cabo: no arranque ficou no hotspot (`eth1`) e deixou o `eth0` sem perfil (e, com o cabo ligado, o hotspot ficava sem perfil). Solução na máquina (fora do repositório): perfil próprio por MAC com prioridade, em vez de depender da ordem de chegada:
  ```
  sudo nmcli connection add type ethernet con-name hotspot-usb 802-3-ethernet.mac-address <MAC-do-hotspot> ipv4.method auto ipv6.method ignore connection.autoconnect yes connection.autoconnect-priority 10
  ```
  Desfazer: `sudo nmcli connection delete hotspot-usb`. Ver o resultado com `nmcli -f NAME,DEVICE,STATE connection show | cat` (o `| cat` evita o paginador; sair dele com `q`). Perfil por MAC serve só aquele hotspot: outro modem precisa de outro perfil.
- **Perfil `hotspot-usb` duplicado:** o `nmcli connection add` foi dado em duas mensagens e correu duas vezes; o `modify` por nome falhou com aviso de nome repetido. Dar **um só** comando de criação e verificar com `nmcli ... connection show`; apagar/alterar por UUID quando há duplicados.
- **Hipóteses erradas já descartadas:** `usb-gcode`/`usb_modeswitch` não afetavam o hotspot (o modem USB muda de modo para `cdc_ether` e `eth1` aparece ~5 s depois). Dizer "o cabo não impede o hotspot" foi precipitado: na prática impediu, por causa dos perfis.
- **Teste que dependia do hostname real da máquina** (`test_show_sends_one_script_with_title_and_lines`): passou a usar `show_hostname=False`. Testes não podem depender da máquina onde correm.
- **Cabeçalho da macro com o texto `network_info.cfg`** enganava o `grep` do instalador.
- **Confirmar que os testes falham:** quando se acrescenta um teste, estragar o código de propósito (mutação) e ver o teste falhar. Foi assim que se apanharam testes inúteis.
- **Mainsail mostra as linhas pela ordem inversa** (a mais recente em cima). Não é um erro da macro.
- **Acesso ao GitHub:** o Claude só faz `push` com a app Claude GitHub instalada na conta (Contents read/write), via `https://github.com/apps/claude/installations/new`. Sem isso o push é recusado.
- O `_clean()` do `usb_import.py` (projeto `print-from-usb-klipper`) não remove `;`. Notado, não alterado.

## 7. Convenções

- **Idioma:** conversa comigo em português europeu, direto, sem lisonja. Código, comentários, README, textos do programa (macro, etiquetas, mensagens de erro), nomes de commits e deste repositório: **inglês**. O programa é só em inglês por decisão do autor.
- **Commits:** título curto em inglês no imperativo, corpo a explicar o porquê, versão no título quando muda (`(v0.5.0)`); terminam com as linhas de atribuição dadas pela sessão. Um commit por alteração coerente, `push` direto para `main`.
- **Como trabalhar com o autor:**
  - Propor e esperar o OK antes de alterar ficheiros; uma coisa de cada vez. (Fazer push neste repositório está autorizado, depois de testes verdes.)
  - Explicar ficheiro a ficheiro o que mudou. Terminar a entrega só com os comandos a correr e os serviços a reiniciar, prontos a copiar.
  - Qualidade de engenheiro sénior: verificar antes de mexer, falhar sem deixar rasto, nada de extras sem avisar.
  - Em configuração, hardware ou segurança: dizer o que foi assumido e o que confirmar na documentação oficial.
  - Quando precisa de dados da máquina, pedir **um só comando**, curto, e dizer exatamente o que colar.
  - Nomes de macros Klipper sem dígitos no meio. Valores editáveis ficam no `.cfg`/`moonraker.conf`, não escondidos no código.
  - Não alterar projetos vizinhos (`print-from-usb-klipper`, `klipper-hotspot`) a partir daqui.

## 8. Estado

**Feito e publicado (0.5.0):**
- 0.2.0 macro + componente + instalador/desinstalador, só em inglês.
- 0.3.0 linha `Internet:` (ordem por métrica) e linha VPN opcional.
- 0.3.1 instalador preserva opções do utilizador e escreve `show_vpn: False`.
- 0.4.0 USB só para drivers de tethering; cabo de ficha USB interna = Cabo.
- 0.5.0 linha `Hostname:` com `.local` quando o avahi corre (`show_hostname`).
- 55 testes do componente e 65 verificações do instalador a passar.

**Confirmado em máquina real (Pi 3):**
- Instalação e atualização funcionam; a v0.5.0 mostra `Hostname: printer (printer.local)`, `Cable (eth0)`, `USB (eth1)`, `Wi-Fi (wlan0)`, `VPN (tailscale0)` e `Internet: Cable (eth0), then USB (eth1), then Wi-Fi (wlan0)`.
- Cabo = Cable e hotspot = USB, com cabo e hotspot ligados ao mesmo tempo.
- O perfil `hotspot-usb` dá IP ao hotspot com o cabo ligado quando ativado manualmente.

**Por confirmar:**
- Arranque com cabo e hotspot ligados depois de dar prioridade ao `hotspot-usb` e de apagar o duplicado (o resultado do último reinício não foi comunicado).
- `ip -j route show default` real; classificação no Pi 5; `uninstall.sh` numa máquina real.

**Por fazer:**
1. Wi-Fi com nome da rede e intensidade do sinal (`nmcli` e `/proc/net/wireless`). Aprovado pelo autor.
2. Notificações automáticas quando a rede muda (evento `machine:net_state_changed` do Moonraker, com atraso para não repetir), com opção para ligar e desligar. **Por decidir:** ligadas ou desligadas por omissão.
3. Licença do repositório público (`LICENSE`; proposta: MIT). Sem resposta.
4. Documentar no README o perfil `hotspot-usb` (secção 6), por ser o que faz o hotspot funcionar com o cabo.

**Problemas conhecidos / limitações:**
- O Update Manager do Mainsail não volta a correr o `install.sh`: a cópia da macro e o bloco do `moonraker.conf` só se refrescam com `bash install.sh`.
- `Internet:` mostra a ordem configurada, não se a ligação tem dados (um hotspot sem saldo aparece na mesma).
- Prioridade cabo > hotspot > Wi-Fi não é garantida pelo programa; depende dos perfis do NetworkManager de cada máquina. Com o hotspot à frente do Wi-Fi gastam-se dados móveis; mudá-lo é um projeto à parte e arriscado (não iniciado).
- A falha original de o `eth1` não aparecer uma vez (primeiro dia) não foi reproduzida.
