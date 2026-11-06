#!/usr/bin/env python3
"""Generate a local WAV and WhatsApp-ready Opus file; never sends messages."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text-file', required=True, type=Path)
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/zap/voice.json')
    parser.add_argument('--speed', type=float)
    parser.add_argument('--volume', type=float)
    parser.add_argument('--room-noise', type=float, help='pink room-noise amplitude, 0 to disable')
    parser.add_argument('--background', type=Path, help='loop this ambience WAV/OGG under the voice')
    parser.add_argument('--background-volume', type=float, default=0.08)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    speed = args.speed if args.speed is not None else config['speed']
    volume = args.volume if args.volume is not None else config['volume']
    room_noise = args.room_noise if args.room_noise is not None else config.get('room_noise_amplitude', 0.0)
    if not 0.6 <= speed <= 1.5 or not 0.5 <= volume <= 2.0:
        parser.error('speed must be 0.6–1.5; volume must be 0.5–2.0')
    if not 0 <= room_noise <= 0.05:
        parser.error('room-noise must be 0–0.05')
    if not 0 <= args.background_volume <= 0.3:
        parser.error('background-volume must be 0–0.3')
    if args.background and not args.background.is_file():
        parser.error(f'background not found: {args.background}')
    transcript = args.text_file.read_text().strip()
    if not transcript:
        parser.error('empty transcript')
    key = os.environ.get('CARTESIA_API_KEY') or os.environ.get('CARTESIA')
    if not key:
        parser.error('load ~/.config/zap/cartesia.env first (CARTESIA_API_KEY)')
    if not shutil.which('ffmpeg'):
        parser.error('ffmpeg required for Opus conversion')
    os.umask(0o077)
    parent = args.output_dir
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='zap-voice-', dir=parent))
    payload = {
        'model_id': config['model_id'],
        'voice': config['voice_id'],
        'language': config['language'],
        'transcript': transcript,
        'generation_config': {'speed': speed, 'volume': volume},
        'output_format': {'container': 'wav', 'encoding': 'pcm_s16le', 'sample_rate': 44100},
    }
    req = urllib.request.Request('https://api.cartesia.ai/tts/bytes',
        data=json.dumps(payload).encode(), headers={
            'Authorization': 'Bearer '+key,
            'Cartesia-Version': config['api_version'],
            'Content-Type': 'application/json',
        })
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        print(f'Cartesia HTTP {exc.code}: '+exc.read().decode(errors='replace').replace(key, '[REDACTED]'), file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f'Cartesia request failed: {type(exc).__name__}; no automatic retry', file=sys.stderr)
        return 1
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        print('Cartesia did not return a WAV; output rejected', file=sys.stderr)
        return 1
    wav, ogg = out/'audio.wav', out/'audio.ogg'
    wav.write_bytes(data)
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(wav)]
    if room_noise or args.background:
        # Quiet, filtered pink noise. amix ends at the voice duration.
        filters = '[0:a]aresample=48000[voice];'
        inputs = 1
        if room_noise:
            filters += (f'anoisesrc=color=pink:amplitude={room_noise}:r=48000:seed=42,'
                'highpass=f=120,lowpass=f=6500[room];')
            inputs += 1
        if args.background:
            command += ['-stream_loop', '-1', '-i', str(args.background)]
            filters += f'[1:a]aresample=48000,volume={args.background_volume}[amb];'
            inputs += 1
        labels = '[voice]' + ('[room]' if room_noise else '') + ('[amb]' if args.background else '')
        filters += (f'{labels}amix=inputs={inputs}:duration=first:normalize=0,'
            'alimiter=limit=0.98[out]')
        command += ['-filter_complex', filters, '-map', '[out]']
    command += ['-ac', '1', '-ar', '48000', '-c:a', 'libopus', '-b:a', '32k',
        '-application', 'voip', str(ogg)]
    subprocess.run(command, check=True)
    (out/'request.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    (out/'effects.json').write_text(json.dumps({'room_noise_amplitude': room_noise,
        'background': str(args.background) if args.background else None,
        'background_volume': args.background_volume}, indent=2))
    print(json.dumps({'wav': str(wav), 'ogg': str(ogg), 'request': str(out/'request.json'),
        'speed': speed, 'volume': volume, 'room_noise_amplitude': room_noise}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
