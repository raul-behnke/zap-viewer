# Enviar, editar e apagar

Estas ações alteram o WhatsApp real. Sempre resolva o chat primeiro e use o
nome exatamente como aparece no arquivo em `confirm_name`. Chat sem nome usa o
próprio `chat_id`. `allow_group=true` só quando o dono pediu o grupo;
`allow_ambiguous=true` só depois que ele escolheu entre homônimos.

Não chame o WAHA diretamente. Use apenas a API local, que aplica as travas e
registra todas as tentativas.

## Enviar texto

Um pedido explícito autoriza o envio. Mostre em uma atualização o destinatário
resolvido e o texto exato e então envie, sem pedir aprovação adicional quando
não houver ambiguidade.

Monte JSON com `jq`, para preservar aspas e quebras de linha:

```bash
ZAP_PAYLOAD="$(jq -n \
  --arg chat_id "$CHAT_ID" \
  --arg text "$MESSAGE_TEXT" \
  --arg confirm_name "$CONFIRM_NAME" \
  '{chat_id:$chat_id,text:$text,confirm_name:$confirm_name}')"
curl -sS --fail-with-body --max-time 120 -X POST \
  "$ZAP_URL/api/send/text" \
  -H 'Content-Type: application/json' \
  --data-binary "$ZAP_PAYLOAD"
```

O endpoint de mídia é `POST /api/send/media`, multipart, com `chat_id`,
`confirm_name`, `file`, `allow_group?`, `allow_ambiguous?` e `caption?`. O teto
é 64 MB.

## Editar

Endpoint local:

`PUT /api/chats/{chat_id}/messages/{message_id}`

Corpo: `text`, `confirm_name`, `allow_group?`, `allow_ambiguous?`.

Antes de chamar:

1. Leia a mensagem pelo arquivo e confirme chat, autor, horário e texto atual.
2. Só prossiga quando `from_me: true`, `revoked_at: null` e a mensagem tiver no
   máximo 15 minutos.
3. Mostre destinatário, texto atual e texto novo. Pedido explícito e inequívoco
   autoriza a edição; se a mensagem ou o texto estiverem incertos, pergunte.

Depois, releia a thread. O retorno `verification: pending_webhook` significa
que o WhatsApp aceitou a chamada, mas o arquivo ainda aguarda o evento de
confirmação.

## Apagar para todos

Endpoint local:

`DELETE /api/chats/{chat_id}/messages/{message_id}`

Corpo: `confirm_name`, `confirm_message_id`, `allow_group?`,
`allow_ambiguous?`. `confirm_message_id` deve repetir exatamente o ID do alvo.

Antes de chamar:

1. Leia e mostre destinatário, horário e conteúdo da mensagem.
2. Só prossiga quando `from_me: true`, `revoked_at: null` e a mensagem tiver no
   máximo dois dias.
3. Peça confirmação explícita imediatamente antes. O pedido anterior para
   enviar não autoriza uma exclusão posterior.

Depois, releia a thread e procure `revoked_at`. A exclusão é para todos no
WhatsApp, mas o arquivo local preserva texto e mídia. Não existe ação suportada
para “apagar só para mim”. Nunca use a rota do WAHA que limpa o chat inteiro.

## Histórico das ações

`GET /api/message-actions` aceita `chat_id`, `message_id` e `limit`. Ele mostra
tentativas aceitas, recusadas e incertas de edição ou exclusão, inclusive o
texto anterior e o novo texto.

## Resultado incerto e erros

- `503 ... outcome unknown ...`: a ação pode ter acontecido. Confira a thread
  e o histórico; nunca repita automaticamente.
- `404 unknown chat_id`: refaça a resolução do chat.
- `404 unknown message_id`: a mensagem não pertence ao chat ou não está no
  arquivo.
- `403 only messages sent by the owner can be changed`: não contorne.
- `409 confirm_name does not match`: destinatário errado; resolva novamente.
- `409 ... group ... allow_group=true`: só prossiga se o dono pediu o grupo.
- `409 ... chats are named ...`: peça que o dono escolha o chat.
- `409 ... window expired`: o prazo do WhatsApp terminou.
- `409 message is already revoked`: não tente de novo.
- `409 confirm_message_id ...`: a confirmação não corresponde ao alvo.
- `413 file over 64 MB`: comprima ou divida a mídia antes de nova tentativa.

Toda tentativa fica registrada. Uma recusa é sinal para corrigir os dados, não
para variar nomes ou IDs no chute.
