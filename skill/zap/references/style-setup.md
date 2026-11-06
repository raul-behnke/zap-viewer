# Gerar o perfil de escrita do dono

Produz `~/.config/zap/communication-style.md`, lido antes de redigir qualquer
mensagem em nome do dono. Rode depois que o backfill tiver trazido histórico
(`/api/overview`); com poucas mensagens o perfil sai raso, e isso deve ser dito.

## Coletar

1. Liste os chats com `GET /api/chats?limit=100` e escolha até 15 com
   `last_msg_ts` recente, misturando contatos pessoais, clientes e grupos.
2. Em cada um, leia até 200 mensagens e fique só com `from_me: true` e
   `type` de texto. Ignore mensagens encaminhadas, links soltos e textos
   colados de terceiros.
3. Junte pelo menos 300 mensagens. Se não chegar a isso, use o que houver e
   registre o volume no arquivo.

Não copie mensagens inteiras para o perfil. Ele descreve padrões; exemplos
curtos e anônimos só quando um padrão não ficar claro sem eles.

## Observar

- Tamanho típico das mensagens e se o dono quebra uma ideia em várias.
- Registro: formal ou informal, “você” ou “vc”, abreviações (“pra”, “tá”).
- Como abre e fecha conversa; se cumprimenta por horário.
- Pontuação, caixa, uso de áudio em vez de texto.
- Emoji: frequência, quais, em que posição, com quem.
- Diferença de tratamento entre família, amigos e clientes. Registre o padrão
  por tipo de relação, nunca apelidos ou intimidades de um contato específico.

## Escrever

Use estas seções, curtas e em português:

```markdown
# Jeito de comunicar do dono

Perfil gerado em AAAA-MM-DD a partir de N mensagens enviadas em M chats.
Pedido explícito e texto literal do dono têm precedência.

## Escolhas de escrita
## Saudação
## Emoji
## Por tipo de relação
## Áudio            (só se houver voz configurada)
## Conferência antes de enviar
```

Grave com permissão `600`. Mostre ao dono um resumo de cinco linhas e pergunte
o que ajustar; aplique as correções dele no arquivo e registre a data do ajuste.
Preferências que ele der depois, em qualquer conversa, também vão para o
arquivo.
