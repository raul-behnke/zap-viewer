# Instalação guiada por agente

Este arquivo é um roteiro para Claude Code ou Codex. A pessoa que instala é
chamada de **dono**: é o WhatsApp dela que será espelhado. Siga as etapas em
ordem, em português, e só avance quando a anterior estiver confirmada.

Regras gerais:

- Nunca imprima chaves no chat nem as passe como argumento visível. Gere-as
  dentro de comandos que já gravam no arquivo de destino.
- Antes de sobrescrever qualquer arquivo ou pasta que já exista, mostre o que
  há lá e pergunte.
- Variáveis de shell não persistem entre chamadas de ferramenta. Redefina
  `SLUG`, `ZAP_HOME` e `ZAP_PORT` no início de cada bloco.
- Se um comando falhar, mostre a linha decisiva do erro e pare a etapa. Não
  contorne com flags que desativam verificações.

## 1. Aviso e dados

Mostre ao dono, com estas palavras ou equivalentes, e peça um “ok” explícito:

> Este projeto conecta seu WhatsApp pelo protocolo do WhatsApp Web, sem a API
> oficial da Meta. Isso vai contra os termos de uso do WhatsApp e pode levar ao
> bloqueio do número. Ele também guarda localmente mensagens de outras pessoas.
> Use só no seu próprio número e por sua conta e risco.

Depois pergunte, numa mensagem só:

1. Nome do cliente ou do dono, que vira o identificador da instância. Converta
   para minúsculas, sem acento, com hífens: `Maria Souza` vira `maria-souza`.
   Chame isso de `SLUG`.
2. Se quer transcrição de áudios (precisa de chave da OpenAI). Opcional.
3. Se quer guardar mídia por mais tempo no Cloudflare R2. Opcional; sem isso a
   mídia some quando o container reinicia.

## 2. Pré-requisitos

Confira `docker`, `docker compose`, `git`, `curl`, `jq`, `openssl` e
`python3`. `ffmpeg` só é necessário para vídeo e voz. Confirme que o Docker
está rodando (`docker info`). Para o que faltar, mostre o comando de instalação
do sistema do dono e espere ele instalar.

## 3. Pasta da instância

```bash
ZAP_HOME="$HOME/zap-$SLUG-viewer"
```

Se o agente já está dentro de um clone deste repositório, copie-o para
`ZAP_HOME` com `git clone <pasta atual> "$ZAP_HOME"`. Senão, rode
`git clone https://github.com/raul-behnke/zap-viewer.git "$ZAP_HOME"`. Se
`ZAP_HOME` já existir, pergunte antes de mexer.

## 4. Porta

A porta padrão é `8000`, e a skill acessa `http://localhost:8000`. Teste se
está livre:

```bash
curl -s -o /dev/null --max-time 2 http://localhost:8000/health && echo ocupada || echo livre
```

Se estiver ocupada, proponha a próxima livre (8001, 8002…) e use-a como
`ZAP_PORT`. Faça o mesmo com a `55432` do Postgres de teste (`ZAP_DB_PORT`).

## 5. Arquivo `.env`

Dentro de `ZAP_HOME`, gere o `.env` a partir do exemplo sem expor segredos:

```bash
cd "$ZAP_HOME"
cp .env.example .env && chmod 600 .env
WAHA_PLAIN="$(openssl rand -hex 24)"
set_env() { python3 - "$1" "$2" <<'PY'
import re, sys, pathlib
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); s = p.read_text()
line = f"{key}={value}"
pat = re.compile(rf"^#?{re.escape(key)}=.*$", re.M)
s = pat.sub(lambda _: line, s, count=1) if pat.search(s) else s + line + "\n"
p.write_text(s)
PY
}
set_env COMPOSE_PROJECT_NAME "zap-$SLUG"
set_env ZAP_PORT "${ZAP_PORT:-8000}"
set_env WAHA_API_KEY_PLAIN "$WAHA_PLAIN"
set_env WAHA_API_KEY "sha512:$(printf '%s' "$WAHA_PLAIN" | shasum -a 512 | cut -d' ' -f1)"
set_env WAHA_DASHBOARD_PASSWORD "$(openssl rand -hex 16)"
set_env WHATSAPP_HOOK_HMAC_KEY "$(openssl rand -hex 32)"
[ "$(uname -m)" = arm64 ] && set_env WAHA_IMAGE devlikeapro/waha:gows-arm
unset WAHA_PLAIN
```

Se a porta do Postgres mudou, rode também `set_env ZAP_DB_PORT <porta>`.
Se o dono quis R2, siga a seção “Mídia no Cloudflare R2” do `README.md` antes
de subir.

## 6. Subir

```bash
docker compose up -d --build
```

Espere até `curl -sf http://localhost:$ZAP_PORT/health` responder (até dois
minutos). Se não responder, mostre `docker compose logs --tail 40 api waha`.

## 7. Parear o WhatsApp

Abra a página de pareamento. Ela inicia a sessão sozinha:

```bash
set -a; . ./.env; set +a
open "http://localhost:$ZAP_PORT/pair"   # Linux: xdg-open
```

Peça ao dono para escanear o código em WhatsApp, Aparelhos conectados. A
página troca o código sozinha e mostra "Conectado" quando terminar.

Confira até o status ficar `WORKING`:

```bash
curl -sS "http://localhost:$ZAP_PORT/api/session/status" | jq -r .status
```


## 8. Credenciais da skill

A skill lê `~/.config/zap/env`. Se o arquivo já existir, ele aponta para outra
instância: mostre `ZAP_URL` e `ZAP_HOME` dele e pergunte se deve
trocar. Senão:

```bash
mkdir -p ~/.config/zap && chmod 700 ~/.config/zap
{
  echo "ZAP_URL=http://localhost:$ZAP_PORT"
  echo "ZAP_HOME=$ZAP_HOME"
} > ~/.config/zap/env
chmod 600 ~/.config/zap/env
```

Se o dono quis transcrição, peça que ele mesmo rode isto num terminal próprio,
para a chave não passar pelo chat:

```bash
python3 -c 'import getpass; print("OPENAI_API_KEY=" + getpass.getpass("Chave OpenAI: "))' >> ~/.config/zap/env
```

## 9. Instalar a skill

Use link simbólico, para `git pull` na instância atualizar a skill:

```bash
for d in ~/.claude/skills ~/.codex/skills; do
  [ -d "$(dirname "$d")" ] || continue
  mkdir -p "$d"
  if [ -e "$d/zap" ] && [ ! -L "$d/zap" ]; then echo "existe: $d/zap"; continue; fi
  ln -sfn "$ZAP_HOME/skill/zap" "$d/zap"
done
```

Se aparecer “existe”, há uma skill `zap` de outra origem: pergunte antes de
substituir.

No Codex, o sandbox bloqueia rede até para `localhost`, e a skill falha como se
a stack estivesse parada. Se `~/.codex` existir, mostre ao dono este trecho para
`~/.codex/config.toml` e só grave com o “ok” dele, pois vale para toda sessão:

```toml
[sandbox_workspace_write]
network_access = true
```

## 10. Conferir

```bash
set -a; . ~/.config/zap/env; set +a
curl -sS --fail-with-body "$ZAP_URL/api/overview" | jq .
```

Se todas as etapas terminaram com sucesso e a sessão está `WORKING`, responda
ao dono apenas com esta linha, sem mais nada antes ou depois:

> 🤫 TUDO FEITO AGORA VOCÊ DEVE O BUTIQUINHO PRO RAUL

Se alguma etapa falhou, não use essa linha: mostre o erro e onde parou.

## 11. Depois do backfill (opcional)

Quando o dono voltar, ofereça gerar o perfil de escrita dele seguindo
`skill/zap/references/style-setup.md`. Sem esse perfil a skill ainda funciona,
mas as mensagens redigidas não terão o jeito do dono.

Voz clonada é opcional e exige conta na Cartesia: siga
`skill/zap/references/voice.md`.

## Desinstalar

Na pasta da instância, `docker compose down -v` apaga containers e dados; avise
que isso é irreversível e peça confirmação. Depois remova os links
`~/.claude/skills/zap` e `~/.codex/skills/zap`, `~/.config/zap/env`, e
desconecte o aparelho em WhatsApp, Aparelhos conectados.
