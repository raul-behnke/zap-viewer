# Voz do dono — Cartesia (opcional)

Só use se `~/.config/zap/voice.json` e `~/.config/zap/cartesia.env` existirem.
O `voice_id` em `voice.json` é um clone autorizado pelo próprio dono; não crie,
substitua ou recrie clones sem pedido. Gerar a voz envia o texto à Cartesia;
considere isso com conteúdo privado ou sensível.

`cartesia.env` (permissão 600) define `CARTESIA_API_KEY`. Não imprima a chave
nem a coloque em scripts, argumentos ou arquivos de áudio.

Formato de `voice.json`:

```json
{
  "voice_id": "ID do clone na Cartesia",
  "model_id": "sonic-3.6-2026-08-27",
  "api_version": "2026-09-17",
  "language": "pt-BR",
  "speed": 1.0,
  "volume": 1.0,
  "room_noise_amplitude": 0.0,
  "calibration_status": "pending"
}
```

## Gerar

Antes de escrever, leia `~/.config/zap/communication-style.md`. Escreva o texto
em arquivo UTF-8 temporário, em fala contínua, sem emoji, tags de pausa ou
instruções cênicas: tudo no arquivo é lido em voz alta. Preserve intenção,
valores e nomes.

```bash
set -a; . ~/.config/zap/env; . ~/.config/zap/cartesia.env; set +a
python3 "$ZAP_HOME/skill/zap/scripts/voice.py" --text-file "$ZAP_TMP_DIR/mensagem.txt"
```

Requer Python 3 e ffmpeg. Produz um diretório temporário privado com WAV,
OGG/Opus mono e `request.json` (texto e parâmetros, sem chave); a saída JSON
informa os caminhos. `--speed` aceita 0.6–1.5 e `--volume` 0.5–2.0;
`--output-dir` define o diretório pai.

## Calibrar

Enquanto `calibration_status` for `pending`, gere três prévias do mesmo texto
com `--speed 0.95`, `1.0` e `1.08` e peça ao dono que escolha. Mude uma
variável por vez. Não alegue ter ouvido o áudio. Depois da escolha, grave em
`voice.json` a velocidade, `calibration_status: "approved_by_owner"` e a data.

A API não tem controle de `pitch`; não invente `stability`, `similarity` ou
parâmetros de outros provedores. Emoção é experimental e documentada só em
inglês: em português, omita. Parâmetros vão a cada geração; o script aplica o
perfil local em toda chamada.

`room_noise_amplitude` mistura ruído rosa no OGG (o WAV fica limpo). Começa em
0; só ative com escolha do dono, depois de comparar WAV e OGG.

## Enviar

Somente quando houver pedido de envio, resolva o chat e siga
[message-actions.md](message-actions.md). Use o arquivo já gerado, sem
regenerar depois da prévia:

```bash
set -a; . ~/.config/zap/env; set +a
curl -sS --fail-with-body --max-time 120 -X POST "$ZAP_URL/api/send/media" \
  --form-string "chat_id=$CHAT_ID" \
  --form-string "confirm_name=$CONFIRM_NAME" \
  -F "file=@${AUDIO_OGG};type=audio/ogg"
```

Não use caption: o backend a ignora para áudio e envia como mensagem de voz.
Não há `reply_to` para mídia. Em timeout, confira o histórico antes de
qualquer nova tentativa.

Depois da prévia ou do envio, apague o diretório `zap-voice-*` criado para a
tarefa, conferindo o caminho e que nenhum arquivo ainda será usado.

## Fontes

- https://docs.cartesia.ai/build-with-cartesia/capability-guides/volume-speed-emotion
- https://docs.cartesia.ai/build-with-cartesia/tts-models/latest
- https://docs.cartesia.ai/api-reference/tts/bytes
