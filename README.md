# zap-viewer

Arquivo pessoal do WhatsApp com busca em português, visualizador ao vivo e uma
skill (`/zap`) que deixa Claude Code e Codex consultar conversas, ler mídia,
transcrever áudios e enviar mensagens com travas de destinatário.

WAHA (engine GOWS) → webhook → FastAPI → Postgres. Tudo roda na sua máquina,
em `http://localhost:8000`.

> **Aviso.** O WAHA usa o protocolo do WhatsApp Web, não a API oficial da Meta.
> Isso vai contra os termos de uso do WhatsApp e pode levar ao bloqueio do
> número. O arquivo guarda mensagens de outras pessoas. Use só no seu número e
> por sua conta e risco.

## Instalar

Abra o Claude Code ou o Codex e cole:

```text
Leia https://raw.githubusercontent.com/raul-behnke/zap-viewer/main/INSTALL.md e siga as
instruções para instalar o zap-viewer para mim. Meu nome é <SEU NOME>.
```

O agente cria `~/zap-<seu-nome>-viewer`, gera as chaves, sobe os containers,
abre a página do QR, grava `~/.config/zap/env` e instala a skill `/zap`.

Para instalar à mão, siga o mesmo `INSTALL.md`.

## Usar

`http://localhost:8000` abre o visualizador. No agente, peça coisas como
“o que eu combinei com o Marcos sobre a entrega?” ou “manda pra Ana que chego
em 10 minutos”. A skill sempre mostra destinatário e texto antes de enviar.

## Endpoints

Sem chave: a API só escuta em localhost e recusa pedidos vindos de outros sites.

| Método | Rota | O que faz |
|---|---|---|
| GET | `/api/overview` | sessão, backfill e resumos |
| GET | `/api/session/status` | estado da sessão |
| POST | `/api/session/start` | inicia a sessão |
| GET | `/api/session/qr` | PNG do QR code |
| GET | `/api/chats?limit&offset&q` | lista de conversas |
| GET | `/api/chats/{chat_id}/messages?limit&before_ts` | thread |
| GET | `/api/search?q&chat_id&limit` | busca full-text pt-BR |
| GET | `/api/media/{message_id}` | mídia (410 se expirou) |
| GET | `/api/events` | SSE ao vivo |
| POST | `/api/send/text` | `{chat_id, text, confirm_name, allow_group?, allow_ambiguous?, reply_to?}` |
| POST | `/api/send/media` | multipart `chat_id`, `confirm_name`, `file`, `allow_group?`, `allow_ambiguous?`, `caption?` |
| PUT | `/api/chats/{chat_id}/messages/{message_id}` | edita mensagem própria em até 15 min |
| DELETE | `/api/chats/{chat_id}/messages/{message_id}` | apaga para todos em até 2 dias |
| GET | `/api/message-actions?chat_id&message_id&limit` | auditoria de edições e exclusões |
| POST | `/api/backfill/start` | dispara um passe |
| GET | `/api/backfill/status` | progresso |

Envio exige `confirm_name` igual ao nome do chat; grupo exige
`allow_group=true` e homônimos exigem `allow_ambiguous=true`. Toda tentativa,
aceita ou recusada, fica registrada.

## Mídia no Cloudflare R2

Sem R2, a mídia fica no disco do container do WAHA e some a cada reinício.
Para guardar:

1. Descomente as variáveis `WAHA_S3_*` no `.env` e preencha endpoint, bucket e
   chaves do R2. Mantenha `WHATSAPP_FILES_LIFETIME=0`.
2. Crie o bucket e a regra de retenção:

   ```bash
   wrangler r2 bucket create zap-media
   wrangler r2 bucket lifecycle add zap-media --expire-days 30
   ```

3. `docker compose up -d`.

## Várias instâncias

Cada instalação tem `COMPOSE_PROJECT_NAME` próprio (volumes separados) e porta
própria (`ZAP_PORT`). A skill fala com a instância apontada em
`~/.config/zap/env`.

## Limites

- O histórico é negociado no pareamento (até 365 dias). Mudar
  `WAHA_GOWS_DEVICE_HISTORY_SYNC_*` exige escanear o QR de novo.
- O GOWS não avisa quando terminou o histórico; o backfill repassa de hora em
  hora nas primeiras 24h.
- Áudio e vídeo não entram na busca; a skill transcreve sob pedido.

## Testes

```bash
docker compose up -d postgres
docker compose exec postgres psql -U zap -d zap -c "CREATE DATABASE zap_test"
pytest -v
```
