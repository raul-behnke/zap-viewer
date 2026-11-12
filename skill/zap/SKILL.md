---
name: zap
description: Consulta o arquivo local do WhatsApp do dono e envia, edita ou apaga mensagens solicitadas, incluindo mídias e áudios. Use quando o pedido depender de conversas, histórico ou anexos do WhatsApp, ou envolver enviar ou responder pelo WhatsApp. Não use apenas porque a tarefa menciona cliente, contato ou orçamento sem relação com WhatsApp.
---

# WhatsApp do dono

Arquivo local autenticado em `ZAP_URL`, com conversas, mensagens e mídia,
servido pelo `zap-viewer` deste repositório. O histórico é parcial: começa no
limite negociado no pareamento. Consulte `/api/overview` quando a cobertura ou
a sincronização importarem; não repita contagens fixas.

## Credenciais e disponibilidade

Em cada novo bloco de shell:

```bash
set -a; . ~/.config/zap/env; set +a
```

Isso define `ZAP_URL` e, se configurada, `OPENAI_API_KEY`.
Nunca escreva chaves inline. Se o arquivo não existir, pare e indique o
`INSTALL.md` do repositório; não procure credenciais em outros lugares. Se
houver `connection refused`, informe que a stack pode estar parada e sugira
`docker compose up -d` na pasta da instância (`ZAP_HOME` no mesmo arquivo),
mas não a inicie sem pedido.

## Escolher o procedimento

- Para localizar chats, pesquisar, ler threads ou tratar mídia, leia
  [references/search-and-media.md](references/search-and-media.md).
- Para redigir em nome do dono, leia `~/.config/zap/communication-style.md` e
  o histórico recente do destinatário. Se o arquivo não existir, ofereça
  gerá-lo seguindo [references/style-setup.md](references/style-setup.md).
- Para enviar, editar ou apagar, leia obrigatoriamente
  [references/message-actions.md](references/message-actions.md).
- Para gerar áudio com a voz do dono, leia
  [references/voice.md](references/voice.md). Só existe se
  `~/.config/zap/voice.json` estiver configurado.

## Regra central de busca

A busca é literal, não semântica. Um resultado vazio para uma palavra não prova
que a conversa não existe.

- Resultado positivo: abra as mensagens próximas e confirme o contexto.
- Resultado vazio: tente sinônimos, jargão usado na época, nomes, valores,
  endereços, CNPJ ou números de pedido antes de dizer que não encontrou.
- Ao não encontrar, diga quais termos, chats e períodos foram tentados.
- Áudio e vídeo não entram na busca. Se houver mídia no período, avise que a
  informação pode estar ali e ofereça transcrever os arquivos disponíveis.

## Regra central de destinatário

Antes de qualquer envio, edição ou exclusão, resolva o chat por
`GET /api/chats?q=`. Se houver mais de um candidato plausível, mostre nome,
tipo de chat e data da última mensagem e peça ao dono que escolha. Nunca decida
entre homônimos por conta própria.

## Ações que alteram o WhatsApp

- Um pedido explícito de envio autoriza o envio; mostre antes o destinatário e
  o conteúdo exato, sem criar uma fila de aprovação adicional.
- Edição só pode atingir mensagem enviada pelo dono e dentro de 15 minutos.
- Exclusão significa “apagar para todos”, só pode atingir mensagem enviada pelo
  dono e exige confirmação explícita imediatamente antes da chamada.
- Nunca ofereça “apagar só para mim” nem apagar a conversa inteira.
- Timeout, `503` ou falha de transporte deixam o resultado incerto. Confira a
  thread; nunca repita automaticamente.
- Apagar no WhatsApp marca `revoked_at`, mas preserva texto e mídia no arquivo
  local. Explique essa diferença quando for relevante.

## Limites honestos

- O backfill não cobre necessariamente todos os chats; consulte `/api/overview`.
- Áudios e vídeos não são pesquisáveis pelo texto.
- `media_archived: false` indica mídia perdida no armazenamento temporário.
- `media_archived: true` com `410` indica expiração no armazenamento permanente.
- `edited_at` registra edição e `revoked_at` registra exclusão para todos.
