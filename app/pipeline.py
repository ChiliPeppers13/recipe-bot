"""Пайплайн: ссылка на видео -> аудио -> текст -> рецепт (JSON).

Три шага, каждый можно вызвать отдельно:
    python pipeline.py <url>            # всё сразу, результат в recipe.json
    python pipeline.py <url> --keep     # оставить аудио для отладки

Зависимости: yt-dlp и ffmpeg в системе, faster-whisper в питоне.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CACHE_DIR = Path.home() / ".hermes" / "cache" / "recipe-bot"

# --- настройки -------------------------------------------------------------

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")   # tiny/base/small/medium
WHISPER_LANG = os.getenv("WHISPER_LANG", "")          # "" = автоопределение
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4.1-flash")
LLM_BASE = os.getenv("LLM_BASE", "https://opencode.ai/zen/go/v1")

MAX_AUDIO_SECONDS = 20 * 60   # защита: не транскрибируем двухчасовые стримы


# --- утилиты ---------------------------------------------------------------

def load_env() -> None:
    """Подхватываем ключи из ~/.hermes/.env и локального .env, если они есть."""
    for path in (Path.home() / ".hermes" / ".env", Path(__file__).parent.parent / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} упал:\n{res.stderr[-1500:]}")
    return res.stdout


# --- 1. скачивание ---------------------------------------------------------

def download_audio(url: str, workdir: Path) -> tuple[Path, dict]:
    """Тянем только звук (без видео — быстрее и легче) и приводим к 16 кГц моно.

    Важно: явный -f bestaudio. Без него yt-dlp берёт «лучшее» видео,
    а 1080p-форматы YouTube отдаёт только залогиненным → 'Authentication required'.
    """
    probe = run([
        "yt-dlp", "--no-warnings", "--no-playlist", "--skip-download",
        "--print", "%(title)s\t%(uploader,channel)s\t%(duration)s\t%(thumbnail)s\t%(extractor_key)s",
        url,
    ]).strip().split("\t")
    title, author = probe[0], probe[1]
    duration = int(float(probe[2] or 0)) if probe[2] not in ("NA", "") else 0
    thumbnail = None if probe[3] in ("NA", "") else probe[3]
    extractor = probe[4].lower()

    if duration > MAX_AUDIO_SECONDS:
        raise ValueError(f"видео слишком длинное: {duration // 60} мин")

    src = workdir / "source.%(ext)s"
    run([
        "yt-dlp", "-x", "--audio-format", "wav", "-f", "bestaudio/best",
        "--no-playlist", "--no-warnings", "--no-progress",
        "-o", str(src), url,
    ])
    raw = next(workdir.glob("source.wav"))

    wav = workdir / "audio16k.wav"
    run([
        "ffmpeg", "-y", "-i", str(raw), "-ac", "1", "-ar", "16000",
        "-af", "loudnorm=I=-16:TP=-1.5", str(wav),
    ])
    raw.unlink(missing_ok=True)

    info = {
        "url": url,
        "title": title,
        "author": author,
        "duration": duration,
        "thumbnail": thumbnail,
        "platform": extractor,
    }
    return wav, info


# --- 2. транскрипция -------------------------------------------------------

def transcribe(wav: Path) -> dict:
    from faster_whisper import WhisperModel

    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(wav),
        language=WHISPER_LANG or None,
        vad_filter=True,           # выкидывает тишину и музыку
        beam_size=5,
    )

    chunks, full = [], []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        chunks.append({"start": round(seg.start, 1), "text": text})
        full.append(text)

    return {"language": info.language, "text": " ".join(full), "chunks": chunks}


# --- 3. рецепт из текста ---------------------------------------------------

RECIPE_PROMPT = """Ты — редактор кулинарных рецептов. Тебе дают расшифровку видео, где готовят блюдо, с таймкодами.

Верни СТРОГО JSON-объект такой формы:
{
  "title": "название блюда как в видео",
  "subtitle": "одна короткая строка-описание, до 60 символов",
  "emoji": "один эмодзи блюда",
  "time_min": 30,
  "servings": 2,
  "difficulty": "легко" | "средне" | "сложно",
  "ingredients": [{"name": "спагетти", "qty": "200 г", "est": "200 г"}],
  "steps": [{"n": 1, "text": "что делать, 1-3 предложения", "at": "0:45"}],
  "tips": ["совет от автора, 1 предложение"],
  "missing": ["одна строка про то, чего в видео не хватило"]
}

Правила:
- Ингредиенты и шаги — ТОЛЬКО то, что реально есть в видео. Ничего не выдумывай.
- "qty" — точное количество, если автор его назвал в видео. Если НЕ назвал — строго "по вкусу".
- "est" — твоя разумная оценка количества на указанное число порций, с "≈" (например "≈200 г"). Заполняй ВСЕГДА, даже когда qty точное (тогда повтори его). Это подсказка пользователю, а не слова автора.
- Шаги упорядочены, каждый — одно законченное действие. Не склеивай несколько действий в один шаг.
- В "at" — таймкод из разметки [м:сс], рядом с которым говорится этот шаг. Если не привязан — пустая строка.
- "missing" — максимум 3 пункта, каждый про разное. Не перечисляй по одному ингредиенту за раз: все отсутствующие граммовки — это ОДИН пункт.
- Никакого текста вокруг JSON."""



def _timeline(transcript: dict) -> str:
    """Расшифровка в виде [м:сс] текст — чтобы модель могла ставить таймкоды."""
    lines = []
    for chunk in transcript["chunks"]:
        start = int(chunk["start"])
        lines.append(f"[{start // 60}:{start % 60:02d}] {chunk['text']}")
    return "\n".join(lines)


def make_recipe(transcript: dict, info: dict) -> dict:
    key = os.getenv("OPENCODE_GO_API_KEY")
    if not key:
        raise RuntimeError("нет OPENCODE_GO_API_KEY (положи в .env)")

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": RECIPE_PROMPT},
            {"role": "user", "content": (
                f"Видео: {info.get('title')}\n"
                f"Автор: {info.get('author')}\n\n"
                f"Расшифровка (с таймкодами):\n{_timeline(transcript)[:24000]}"
            )},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    session_id = uuid.uuid4().hex
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # без этого заголовка relay OpenCode Go отвечает 403 MissingSessionID
            "x-opencode-session": f"recipe-bot-{session_id}",
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Recipe Bot",
            "User-Agent": "RecipeBot/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"LLM {e.code}: {detail}") from e

    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        raise RuntimeError(f"LLM вернул пустой ответ (finish={data['choices'][0].get('finish_reason')})")
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    recipe = json.loads(text)

    recipe["source"] = info
    recipe["raw_transcript"] = transcript["text"]
    recipe["language"] = transcript["language"]
    return recipe


# --- main ------------------------------------------------------------------

def build(url: str, keep_audio: bool = False) -> dict:
    load_env()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")

    if cache_file.exists():                      # ту же ссылку второй раз не качаем
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        info, transcript = cached["info"], cached["transcript"]
        print(f"1-2/3 из кэша: {info['title']}", flush=True)
    else:
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes" / "cache" / "scratch") as tmp:
            work = Path(tmp)
            print("1/3 скачиваю звук…", flush=True)
            wav, info = download_audio(url, work)
            print(f"    {info['title']} · {info['duration'] // 60}:{info['duration'] % 60:02d}", flush=True)

            print("2/3 расшифровываю…", flush=True)
            transcript = transcribe(wav)
            print(f"    язык={transcript['language']} · {len(transcript['text'])} символов", flush=True)

            if keep_audio:
                keep = Path.cwd() / "audio16k.wav"
                keep.write_bytes(wav.read_bytes())
                print(f"    аудио сохранено: {keep}", flush=True)

        cache_file.write_text(
            json.dumps({"info": info, "transcript": transcript}, ensure_ascii=False),
            encoding="utf-8",
        )

    print("3/3 собираю рецепт…", flush=True)
    return make_recipe(transcript, info)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python pipeline.py <url> [--keep]")
    result = build(sys.argv[1], keep_audio="--keep" in sys.argv)
    out = Path.cwd() / "recipe.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nготово: {out}")
    print(f"{result.get('emoji','')} {result['title']} · "
          f"{len(result['ingredients'])} ингр. · {len(result['steps'])} шагов")
