#!/usr/bin/env python
"""
tts_watch.py – mappvakt: dokument in i iCloud Drive/tts, ljudbok ut.

Flöde
  iCloud Drive/tts/<fil>            ← du lägger en pdf/epub/txt/md/docx/rtf här
  iCloud Drive/tts/_outputs/<namn>/ ← <namn>.mp3, originalet flyttat hit, log.txt
                                       (progress.txt under körning, raderas när klart)
  iCloud Drive/tts/_outputs/_failed/<namn>/ ← om något gick fel, felet i log.txt

Allt läses av Kokoro (engelsk röst). Språket detekteras bara för loggen.

Topologi
  Vakten (--loop) är en liten process som bara pollar mappen. Varje fil körs i en
  UNDERPROCESS (--process <fil>) som laddar Kokoro, gör jobbet och dör: inget
  torch-minne ligger kvar mellan filerna. Vakten startas av Terminal.app via
  tts-watch.command (launchd-supervisorn öppnar den): Terminal har iCloud-behörigheten,
  en direkt launchd-start hänger på TCC.

Prestanda (lästa ur pdf-narrators extract.py och Kokoros pipeline.py)
  * Städningen lägger radbrytning efter varje mening och Kokoro delar på radbrytning,
    vilket ger ett modellanrop per mening. Här slås meningar ihop till ~CHUNK_CHARS
    tecken före anropet (samma sak Kokoro själv gör för icke-engelska).
  * Kokoro väljer bara cuda/cpu själv; här skickas 'mps' uttryckligen på Apple Silicon.
  * Ljudet strömmas till ffmpeg allteftersom: konstant minne, ingen stor wav-tempfil.
"""
import os, re, sys, time, shutil, subprocess, tempfile, fcntl, traceback
from pathlib import Path
from datetime import datetime

# ── konfiguration ─────────────────────────────────────────────────────────────
WATCH   = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/tts"
OUT     = WATCH / "_outputs"
FAILED  = OUT / "_failed"
LOCK    = Path.home() / "Library/Logs/tts-watch.lock"
EXTS    = {".pdf", ".epub", ".txt", ".md", ".markdown", ".docx", ".rtf"}
STABLE_SECONDS  = 3          # filstorlek måste vara oförändrad så länge (iCloud-synk)
KOKORO_VOICE    = "af_heart"
CHUNK_CHARS     = 400        # meningar slås ihop upp till så här många tecken per anrop
PARAGRAPH_PAUSE = 0.45       # sekunder tystnad mellan stycken
PROGRESS_EVERY  = 120        # sekunder mellan progress.txt-uppdateringar
SAMPLE_RATE     = 24000
REPO   = Path(__file__).resolve().parent
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)

# ── språkdetektion (bara för loggen) ───────────────────────────────────────────
SV = {"och","att","det","är","som","för","med","inte","till","av","den","på","har","ett","om","vi","kan","ska","från","var","här","när","också","eller","men","blir","vid","under","över","efter"}
EN = {"the","and","that","is","for","with","not","to","of","this","on","have","a","an","about","we","can","will","from","was","here","when","also","or","but","be","at","under","over","after"}

def detect_lang(text):
    words = re.findall(r"[a-zåäöA-ZÅÄÖ]+", text.lower())
    if not words: return "en"
    sv = sum(w in SV for w in words) + sum(c in "åäö" for c in text.lower()) * 0.5
    en = sum(w in EN for w in words)
    return "sv" if sv > en else "en"

# ── textextraktion ─────────────────────────────────────────────────────────────
def _clean_keep_paragraphs(raw: str) -> str:
    """pdf-narrators clean_pipeline, fast styckevis så tomraderna (styckegränserna)
    överlever: clean_pipeline filtrerar annars bort dem."""
    from extract import clean_pipeline
    paras = [p for p in re.split(r"\n\s*\n", raw) if p.strip()]
    return "\n\n".join(clean_pipeline(p) for p in paras)

def extract_text(src: Path, workdir: Path) -> str:
    sys.path.insert(0, str(REPO))
    ext = src.suffix.lower()
    if ext in {".txt", ".md", ".markdown"}:
        raw = src.read_text(encoding="utf-8", errors="replace")
        if ext != ".txt":  # strippa enkel markdown-syntax
            raw = re.sub(r"^#{1,6}\s+", "", raw, flags=re.M)
            raw = re.sub(r"[*_`>]+", "", raw)
            raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
        return _clean_keep_paragraphs(raw)
    if ext in {".docx", ".rtf"}:
        out = workdir / "converted.txt"   # macOS inbyggd konvertering
        subprocess.run(["textutil", "-convert", "txt", "-output", str(out), str(src)],
                       check=True, capture_output=True)
        return _clean_keep_paragraphs(out.read_text(encoding="utf-8", errors="replace"))
    if ext in {".pdf", ".epub"}:
        # Upphovsmannens extraktion (TOC, sidhuvud/-fot, städning). Styckena
        # överlever inte den (join_wrapped_lines filtrerar bort tomrader).
        from extract import extract_book
        outdir = workdir / "extracted"
        extract_book(str(src), use_toc=True, extract_mode="whole", output_dir=str(outdir))
        txts = sorted(outdir.rglob("*.txt"))
        if not txts:
            raise RuntimeError("extraktionen gav ingen text (skannad PDF utan OCR?)")
        return "\n\n".join(t.read_text(encoding="utf-8", errors="replace") for t in txts)
    raise ValueError(f"filtyp stöds inte: {ext}")

# ── chunkning: meningar → ~CHUNK_CHARS-bitar, stycken som hårda gränser ───────
def make_chunks(text: str):
    """Ger [(chunk_text, ny_paragraf: bool)]. Efter städningen är varje mening en rad;
    raderna slås ihop upp till CHUNK_CHARS. Dubbel radbrytning = styckegräns."""
    chunks = []
    for pi, para in enumerate(re.split(r"\n\s*\n", text)):
        lines = [l.strip() for l in para.splitlines() if l.strip()]
        buf, first = "", True
        for line in lines:
            if buf and len(buf) + 1 + len(line) > CHUNK_CHARS:
                chunks.append((buf, first and pi > 0)); buf, first = line, False
            else:
                buf = f"{buf} {line}".strip()
        if buf:
            chunks.append((buf, first and pi > 0))
    return chunks

# ── TTS: Kokoro → ffmpeg-ström → mp3 ───────────────────────────────────────────
def kokoro_pipeline():
    # Mätt 5/9 2026 på M-serien: MPS ger ingen vinst (STFT faller tillbaka på CPU,
    # kopieringen äter resten), 1,7x realtid mot CPU:s 1,9x. Därför CPU som standard.
    # Överstyr med TTS_DEVICE=mps om en senare torch/kokoro gör MPS meningsfull.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")  # före torch-importen
    import torch
    from kokoro import KPipeline
    device = os.environ.get("TTS_DEVICE", "cpu")
    threads = int(os.environ.get("TTS_THREADS", "0"))       # 0 = torchs standard (4)
    if threads > 0: torch.set_num_threads(threads)
    try:
        pipe = KPipeline(lang_code="a", device=device)
    except Exception as e:
        log(f"varning: {device} misslyckades ({e}), faller tillbaka på CPU"); device = "cpu"
        pipe = KPipeline(lang_code="a", device="cpu")
    return pipe, device

def synthesize(text: str, mp3: Path, progress_path: Path, loglines: list):
    import numpy as np
    pipe, device = kokoro_pipeline()
    loglines.append(f"enhet: {device}")
    chunks = make_chunks(text)
    total = sum(len(c) for c, _ in chunks) or 1
    done, t0, last_report = 0, time.time(), 0.0
    silence = (np.zeros(int(SAMPLE_RATE * PARAGRAPH_PAUSE), dtype=np.int16)).tobytes()

    def report(final=False):
        el = time.time() - t0
        pct = done / total
        eta = (el / pct - el) if pct > 0.005 else float("nan")
        msg = (f"{pct*100:5.1f} %  ({done:,} / {total:,} tecken)  "
               f"gått {el/60:.0f} min, ~{eta/60:.0f} min kvar  [{device}]")
        progress_path.write_text(f"{datetime.now():%H:%M:%S}  {msg}\n", encoding="utf-8")
        return msg

    ff = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error",
                           "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
                           "-codec:a", "libmp3lame", "-q:a", "4", str(mp3)],
                          stdin=subprocess.PIPE)
    try:
        for chunk_text, new_para in chunks:
            if new_para:
                ff.stdin.write(silence)
            for _, _, audio in pipe(chunk_text, voice=KOKORO_VOICE, split_pattern=None):
                if audio is None: continue
                a = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
                ff.stdin.write((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())
            done += len(chunk_text)
            if time.time() - last_report >= PROGRESS_EVERY:
                last_report = time.time(); log("      " + report())
    finally:
        ff.stdin.close(); rc = ff.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg avslutade med kod {rc}")
    loglines.append(f"anrop: {len(chunks)} chunkar à ≤{CHUNK_CHARS} tecken")
    loglines.append("tid: " + report(final=True))

# ── en fil (körs i underprocess) ───────────────────────────────────────────────
def safe_name(p: Path) -> str:
    n = re.sub(r"[^\w\s.-]", "", p.stem, flags=re.U).strip().replace(" ", "_")
    return n or "namnlos"

def run_file(src: Path):
    name = safe_name(src)
    dest = OUT / name
    i = 2
    while dest.exists():
        dest = OUT / f"{name}-{i}"; i += 1
    dest.mkdir(parents=True)
    progress = dest / "progress.txt"
    t0 = time.time()
    loglines = [f"källa: {src.name}", f"start: {datetime.now():%Y-%m-%d %H:%M:%S}"]
    try:
        with tempfile.TemporaryDirectory() as td:
            text = extract_text(src, Path(td)).strip()
        if len(text) < 20:
            raise RuntimeError(f"för lite text ({len(text)} tecken)")
        loglines += [f"tecken: {len(text):,}", f"språk (detekterat): {detect_lang(text)}"]
        progress.write_text("startar Kokoro …\n", encoding="utf-8")
        synthesize(text, dest / f"{name}.mp3", progress, loglines)
        shutil.move(str(src), str(dest / src.name))
        loglines.append(f"klart: {time.time()-t0:.0f}s")
        (dest / "log.txt").write_text("\n".join(loglines) + "\n", encoding="utf-8")
        progress.unlink(missing_ok=True)
        log(f"KLAR  {src.name} → {dest.relative_to(WATCH)}  ({time.time()-t0:.0f}s)")
    except Exception as e:
        loglines += [f"FEL: {e}", traceback.format_exc()]
        fdest = FAILED / dest.name
        fdest.mkdir(parents=True, exist_ok=True)
        if src.exists(): shutil.move(str(src), str(fdest / src.name))
        (fdest / "log.txt").write_text("\n".join(loglines) + "\n", encoding="utf-8")
        shutil.rmtree(dest, ignore_errors=True)
        log(f"FEL   {src.name}: {e}")
        sys.exit(1)

# ── vakten ─────────────────────────────────────────────────────────────────────
def process(src: Path):
    """Kör filen i en egen process så Kokoro/torch-minnet frigörs efteråt."""
    log(f"START {src.name}")
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--process", str(src)])

def pending():
    if not WATCH.exists(): return []
    out = []
    for p in sorted(WATCH.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in EXTS:
            continue                      # undermappar, .icloud-stubbar, .DS_Store
        s1 = p.stat().st_size
        if s1 == 0: continue
        time.sleep(STABLE_SECONDS)
        if p.exists() and p.stat().st_size == s1:
            out.append(p)
    return out

class ICloudBlocked(Exception): pass
def _alarm(signum, frame): raise ICloudBlocked()

def main():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lf:
        try: fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                        # en annan körning pågår redan
        import signal
        signal.signal(signal.SIGALRM, _alarm); signal.alarm(30)
        try:
            OUT.mkdir(parents=True, exist_ok=True); FAILED.mkdir(exist_ok=True)
            files = pending()
        except ICloudBlocked:
            log("FEL   iCloud Drive-mappen svarar inte (TCC). Processen måste vara startad "
                "av Terminal.app, se tts-watch.command. Avslutar så supervisorn startar om.")
            return "blocked"
        finally:
            signal.alarm(0)
        if not files: return
        log(f"{len(files)} fil(er) i kö")
        for f in files: process(f)

def loop(interval=15):
    log(f"vakten startad, pollar {WATCH} var {interval}:e sekund (pid {os.getpid()})")
    while True:
        try:
            if main() == "blocked":
                sys.exit(3)               # supervisorn startar om via Terminal
        except SystemExit: raise
        except Exception as e: log(f"FEL   oväntat i loopen: {e}")
        time.sleep(interval)

if __name__ == "__main__":
    if "--process" in sys.argv:
        run_file(Path(sys.argv[sys.argv.index("--process") + 1]))
    elif "--loop" in sys.argv:
        loop()
    else:
        main()
