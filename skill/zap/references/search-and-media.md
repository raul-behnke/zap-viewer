# Busca, leitura e mídia

Use `curl -sS --fail-with-body --max-time 30` para que erros HTTP e de conexão
fiquem visíveis. Em consultas, use sempre `-G --data-urlencode`; caracteres
acentuados enviados crus na URL podem ser rejeitados antes da busca.

## Chats

```bash
curl -sS --fail-with-body --max-time 30 -G \
  -H "X-API-Key: $ZAP_API_KEY" "$ZAP_URL/api/chats" \
  --data-urlencode "q=marcos" --data-urlencode "limit=20"
```

`q` procura nome e JID e é sensível a acentos. Se `joao` não achar “João”, use
o nome acentuado ou um trecho sem a letra acentuada. A resposta inclui `id`,
`name`, `is_group`, `last_msg_ts` e `archived`. `name: null` significa que o JID
é a única identificação. Grupo termina em `@g.us`; contato costuma terminar em
`@c.us` ou `@lid`.

## Thread

```bash
curl -sS --fail-with-body --max-time 30 \
  -H "X-API-Key: $ZAP_API_KEY" \
  "$ZAP_URL/api/chats/CHAT_ID/messages?limit=50"
```

As mensagens vêm da mais nova para a mais antiga. Para paginar:

```bash
curl -sS --fail-with-body --max-time 30 -G \
  -H "X-API-Key: $ZAP_API_KEY" \
  "$ZAP_URL/api/chats/CHAT_ID/messages" \
  --data-urlencode "limit=50" \
  --data-urlencode "before_ts=2026-03-01T00:00:00Z"
```

`from_me: true` é o dono falando. Em grupos, `sender_jid` identifica o autor.
Os campos importantes incluem `id`, `ts`, `body`, `type`, `has_media`,
`media_mime`, `media_url`, `media_expires_at`, `media_archived`, `edited_at` e
`revoked_at`.

## Busca

```bash
curl -sS --fail-with-body --max-time 30 -G \
  -H "X-API-Key: $ZAP_API_KEY" "$ZAP_URL/api/search" \
  --data-urlencode "q=orçamento instalação" \
  --data-urlencode "limit=30"
```

Pode restringir com `--data-urlencode "chat_id=..."`. A busca aceita frases
entre aspas, `or` e `-termo`. Os resultados trazem `chat_id`, `chat_name`, `ts`,
`body` e `highlight`. Não responda apenas pelo trecho destacado quando a ordem
ou o contexto da conversa mudarem o sentido.

## Estado geral

```bash
curl -sS --fail-with-body --max-time 30 \
  -H "X-API-Key: $ZAP_API_KEY" "$ZAP_URL/api/overview"
```

Use quando algo parecer faltar ou quando o dono perguntar sobre sincronização.

## Baixar mídia

Crie um diretório privado temporário e não grave mídia no projeto:

```bash
ZAP_TMP_DIR="$(mktemp -d)"
chmod 700 "$ZAP_TMP_DIR"
curl -sS --fail-with-body --max-time 120 \
  -H "X-API-Key: $ZAP_API_KEY" \
  "$ZAP_URL/api/media/MESSAGE_ID" -o "$ZAP_TMP_DIR/arquivo"
```

Abra imagens e PDFs com a ferramenta adequada do ambiente.

### Áudio

Transcrever envia o áudio privado à OpenAI. Faça isso quando o pedido exigir o
conteúdo do áudio. Antes de transcrever mais de 20 arquivos de uma vez, avise
o dono do custo e do volume de dados enviados.

```bash
curl -sS --fail-with-body --max-time 120 \
  https://api.openai.com/v1/audio/transcriptions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@$ZAP_TMP_DIR/audio.ogg" \
  -F model=whisper-1 -F language=pt -F response_format=text
```

Identifique o resultado como transcrição automática, principalmente ao citar
nomes próprios, valores e gírias.

### Vídeo

Extraia frames e áudio dentro do diretório temporário. Ajuste a frequência de
frames à duração do vídeo.

```bash
ffmpeg -v error -i "$ZAP_TMP_DIR/video.mp4" \
  -vf fps=1/10 "$ZAP_TMP_DIR/frame_%03d.jpg"
ffmpeg -v error -i "$ZAP_TMP_DIR/video.mp4" -vn -ar 16000 -ac 1 \
  "$ZAP_TMP_DIR/audio.mp3"
```

Quando terminar de ler, transcrever ou enviar, apague apenas o diretório
temporário criado para a tarefa depois de conferir seu caminho.

## Mídia indisponível

Um `410` usa a mesma mensagem nos dois casos. Consulte a mensagem original:

- `media_archived: false`: o arquivo temporário se perdeu após reinício e não
  pode ser recuperado por nova tentativa.
- `media_archived: true`: o arquivo esteve no R2 e expirou; confira
  `media_expires_at`.
