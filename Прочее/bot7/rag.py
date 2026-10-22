# rag.py — база знаний (RAG) для бота «Грань» (bot6, bot7), отдельный модуль без ВК и LLM.
#
# Книги кладутся в папку (по умолчанию RAG рядом с ботом): txt, doc, docx, pdf (с текстовым
# слоем), epub, fb2, fb2.zip. Модуль разбирает их на фрагменты, строит индекс в SQLite и ищет
# фрагменты по запросу. Поиск — BM25 по леммам (pymorphy3: «учил», «учение» и «учить»
# находят друг друга), при желании вместе с векторным поиском (sentence-transformers,
# multilingual-e5). Ответ по найденным фрагментам пишет LLM бота: build_prompt() даёт строгий
# промпт, parse_answer() разбирает ответ и добавляет источники.
#
#   kb = KnowledgeBase("RAG", "rag_index.db")
#   kb.sync()                       # проиндексировать новые и изменённые книги, убрать удалённые
#   hits = kb.search("чему учил Сократ")
#   system, prompt = build_prompt("чему учил Сократ", hits)
#   answer = parse_answer(llm(system, prompt), hits)   # None — в базе ответа нет
import os
import re
import sys
import json
import math
import time
import array
import shutil
import sqlite3
import logging
import zipfile
import posixpath
import subprocess
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from functools import lru_cache

log = logging.getLogger("gran.rag")

INDEX_VERSION = "1"
SUPPORTED_EXT = (".txt", ".doc", ".docx", ".pdf", ".epub", ".fb2", ".fb2.zip")
CHUNK_CHARS = 1000                 # размер фрагмента
CHUNK_OVERLAP = 200                # перекрытие соседних фрагментов
TOP_K = 6                          # сколько фрагментов отдавать
MIN_COVERAGE = 0.5                 # фрагмент подходит, если в нём есть столько значимых слов запроса
MAX_CONTEXT_CHARS = 8000           # сколько символов фрагментов передавать модели
DENSE_MIN_SIM = 0.82               # порог косинусной близости для векторного поиска (multilingual-e5)
BM25_K1, BM25_B = 1.5, 0.75
NO_ANSWER = "НЕТ_ОТВЕТА"

# служебные слова и слова-«обёртки» вопроса: на поиск не влияют
STOPWORDS = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже
для до его ее если есть еще же за здесь и из или им их к как какой когда кто ли либо между
меня мне много может можно мой мы на над надо наш не него нее нет ни них но ну о об однако он
она они оно от очень по под после потом при про раз с сам свой себя так также такой там тебя
тем то тогда того тоже только том тот ты у уже хотя чего чей чем что чтобы чье эта эти это
этот я ведь вообще который этот сказать говорить расскажи рассказать объяснить объясни найти
найди почему зачем сколько каков какова каковы чём о чем либо нибудь кое бывать
""".split())


# ============ Нормализация слов ============
class Lemmatizer:
    """Слово → лемма. pymorphy3, если установлен; иначе стеммер Snowball; иначе нижний регистр."""

    def __init__(self, kind="auto"):
        self.name, fn = "lower", (lambda w: w)
        if kind in ("auto", "pymorphy3"):
            try:
                import pymorphy3
                morph = pymorphy3.MorphAnalyzer()
                self.name, fn = "pymorphy3", (lambda w: morph.parse(w)[0].normal_form)
            except Exception as e:     # нет пакета или словарей
                log.info("pymorphy3 недоступен (%s) — пробую snowballstemmer", e)
        if self.name == "lower" and kind in ("auto", "snowball"):
            try:
                import snowballstemmer
                self.name, fn = "snowball", snowballstemmer.stemmer("russian").stemWord
            except Exception as e:
                log.info("snowballstemmer недоступен (%s) — слова без нормализации", e)
        self.lemma = lru_cache(maxsize=300000)(lambda w: fn(w).replace("ё", "е"))


WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


def tokenize(text):
    return [w.replace("ё", "е") for w in WORD_RE.findall((text or "").lower())]


def terms(text, lemmatizer, drop_stopwords=True):
    out = []
    for w in tokenize(text):
        if drop_stopwords and w in STOPWORDS:
            continue
        lem = lemmatizer.lemma(w)
        if drop_stopwords and lem in STOPWORDS:
            continue
        out.append(lem)
    return out


# ============ Чтение файлов ============
# Каждый читатель возвращает (название книги или None, [(метка раздела, текст), ...]).
# «Глава 3», «Часть первая. Детство»; строка, кончающаяся как предложение («Книга про логику.»), — не заголовок
HEADING_RE = re.compile(r"^\s*(глава|часть|раздел|книга|chapter|part)\b[^\n]{0,80}(?<![.!?…,;:])\s*$",
                        re.IGNORECASE)


def decode_text(data):
    """Байты текстового файла → строка: BOM, UTF-8, иначе cp1251 или koi8-r (что больше похоже на русский)."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    frequent = set("оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё")
    best, best_score = None, -1
    for enc in ("cp1251", "koi8-r"):
        text = data.decode(enc, errors="replace")
        # в правильной кодировке частые русские буквы — строчные
        score = sum(1 for ch in text[:20000] if ch in frequent)
        if score > best_score:
            best, best_score = text, score
    return best


def split_headings(text, default_label=""):
    """Делит сплошной текст на разделы по строкам вида «Глава 3 …»."""
    sections, label, buf = [], default_label, []
    for line in text.splitlines():
        if HEADING_RE.match(line) and len(line.strip()) <= 90:
            if "".join(buf).strip():
                sections.append((label, "\n".join(buf)))
            label, buf = line.strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((label, "\n".join(buf)))
    return sections


def read_txt(path):
    with open(path, "rb") as f:
        return None, split_headings(decode_text(f.read()))


W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        title = None
        try:
            core = ET.fromstring(z.read("docProps/core.xml"))
            el = core.find("{http://purl.org/dc/elements/1.1/}title")
            title = (el.text or "").strip() or None if el is not None else None
        except KeyError:
            pass
    sections, label, buf = [], "", []
    for p in root.iter(W_NS + "p"):
        parts = []
        for el in p.iter():
            if el.tag == W_NS + "t":
                parts.append(el.text or "")
            elif el.tag == W_NS + "tab":
                parts.append("\t")
            elif el.tag in (W_NS + "br", W_NS + "cr"):
                parts.append("\n")
        line = "".join(parts)
        style = p.find(f"{W_NS}pPr/{W_NS}pStyle")
        style = (style.get(W_NS + "val") or "") if style is not None else ""
        if line.strip() and re.match(r"(heading|заголовок|title)", style, re.IGNORECASE):
            if "".join(buf).strip():
                sections.append((label, "\n".join(buf)))
            label, buf = line.strip()[:90], []
            continue
        buf.append(line)
    if "".join(buf).strip():
        sections.append((label, "\n".join(buf)))
    if len(sections) == 1 and not sections[0][0]:
        sections = split_headings(sections[0][1])
    return title, sections


def _doc_clean(text):
    """Спецсимволы Word: поля (код поля выбрасывается, результат остаётся), ячейки, разрывы."""
    out, depth, in_code = [], 0, []
    for ch in text:
        if ch == "\x13":                       # начало поля: дальше код поля
            in_code.append(True)
        elif ch == "\x14":                     # разделитель: дальше результат поля
            if in_code:
                in_code[-1] = False
        elif ch == "\x15":                     # конец поля
            if in_code:
                in_code.pop()
        elif in_code and in_code[-1]:
            continue
        elif ch in "\r\x0b\x0c\x0e":
            out.append("\n")
        elif ch == "\x07":                     # конец ячейки таблицы
            out.append("\t")
        elif ch == "\x1e":                     # неразрывный дефис
            out.append("-")
        elif ch == "\xa0":
            out.append(" ")
        elif ch < " " and ch not in "\t\n":
            continue                           # картинки и прочие служебные символы
        else:
            out.append(ch)
    return "".join(out)


def read_doc_text(path):
    """Текст документа Word 97–2003 (.doc) по таблице фрагментов (piece table), через olefile."""
    import olefile
    import struct
    with olefile.OleFileIO(path) as ole:
        word = ole.openstream("WordDocument").read()
        ident, = struct.unpack_from("<H", word, 0)
        if ident != 0xA5EC:
            raise ValueError("не документ Word 97–2003")
        flags, = struct.unpack_from("<H", word, 0x0A)
        if flags & 0x0100:
            raise ValueError("документ зашифрован")
        table_name = "1Table" if flags & 0x0200 else "0Table"
        table = ole.openstream(table_name).read()
    ccp_text, = struct.unpack_from("<i", word, 0x4C)
    fc_clx, lcb_clx = struct.unpack_from("<II", word, 0x01A2)
    clx = table[fc_clx:fc_clx + lcb_clx]
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:          # Prc — пропускаем
        cb, = struct.unpack_from("<H", clx, pos + 1)
        pos += 3 + cb
    if pos >= len(clx) or clx[pos] != 0x02:
        raise ValueError("нет таблицы фрагментов")
    lcb, = struct.unpack_from("<I", clx, pos + 1)
    plc = clx[pos + 5:pos + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    parts, total = [], 0
    for i in range(n):
        fc, = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)
        count = cps[i + 1] - cps[i]
        if ccp_text > 0 and total >= ccp_text:
            break
        if ccp_text > 0:
            count = min(count, ccp_text - total)
        if fc & 0x40000000:                              # сжатый фрагмент: 1 байт на символ, cp1252
            start = (fc & 0x3FFFFFFF) // 2
            parts.append(word[start:start + count].decode("cp1252", errors="replace"))
        else:
            parts.append(word[fc:fc + 2 * count].decode("utf-16-le", errors="replace"))
        total += count
    return _doc_clean("".join(parts))


def read_doc(path):
    try:
        text = read_doc_text(path)
    except Exception as e:
        text = None
        for cmd in (["antiword", "-w", "0", path], ["catdoc", "-d", "utf-8", path]):
            if shutil.which(cmd[0]):
                try:
                    text = subprocess.run(cmd, capture_output=True, timeout=120,
                                          check=True).stdout.decode("utf-8", errors="replace")
                    break
                except Exception:
                    continue
        if text is None:
            raise ValueError(f"не удалось прочитать .doc: {e}") from e
    return None, split_headings(text)


def read_pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    title = None
    try:
        title = (reader.metadata.title or "").strip() or None if reader.metadata else None
    except Exception:
        pass
    sections = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            log.warning("%s, стр. %d: %s", path, i, e)
            continue
        # переносы слов в конце строк: «филосо-\nфия» → «философия»
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
        if text.strip():
            sections.append((f"стр. {i}", text))
    if reader.pages and not sections:
        raise ValueError("в PDF нет текстового слоя (скан без распознанного текста)")
    return title, sections


class _HTMLText(HTMLParser):
    """Текст XHTML-главы: блоки — с новой строки, первый заголовок h1–h3 — метка раздела."""
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
              "section", "article", "pre", "dd", "dt", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.heading, self.in_heading = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3") and not self.heading:
            self.in_heading = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.in_heading = False

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data)
        if self.in_heading:
            self.heading = (self.heading + " " + data).strip()

    def text(self):
        return re.sub(r"\n\s*\n+", "\n\n", "".join(self.parts)).strip()


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def read_epub(path):
    with zipfile.ZipFile(path) as z:
        container = _strip_ns(ET.fromstring(z.read("META-INF/container.xml")))
        opf_path = container.find(".//rootfile").get("full-path")
        opf = _strip_ns(ET.fromstring(z.read(opf_path)))
        base = posixpath.dirname(opf_path)
        title_el = opf.find(".//metadata/title")
        title = (title_el.text or "").strip() or None if title_el is not None else None
        manifest = {it.get("id"): it.get("href") for it in opf.iter("item")}
        sections = []
        for ref in opf.iter("itemref"):
            href = manifest.get(ref.get("idref"))
            if not href:
                continue
            name = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
            try:
                raw = z.read(name)
            except KeyError:
                continue
            parser = _HTMLText()
            parser.feed(raw.decode("utf-8", errors="replace"))
            text = parser.text()
            if text and parser.heading:
                sections.append((parser.heading[:90], text))
            elif text:
                sections.extend(split_headings(text))
    return title, sections


def _fb2_text(el, out):
    """Текст элемента fb2: абзацы, стихи, подзаголовки — каждый с новой строки."""
    if el.tag in ("p", "v", "subtitle", "text-author"):
        out.append("".join(el.itertext()).strip())
        return
    for child in el:
        if child.tag in ("title", "section", "binary"):
            continue
        _fb2_text(child, out)


def _fb2_sections(el, sections, prefix=""):
    title_el = el.find("title")
    title = " ".join("".join(p.itertext()).strip() for p in title_el.iter("p")) if title_el is not None else ""
    label = title or prefix
    out = []
    _fb2_text(el, out)
    text = "\n".join(x for x in out if x)
    if text:
        sections.append((label[:90], text))
    for child in el.findall("section"):
        _fb2_sections(child, sections, label)


def parse_fb2(data):
    root = _strip_ns(ET.fromstring(data))
    title_el = root.find("description/title-info/book-title")
    title = (title_el.text or "").strip() or None if title_el is not None else None
    sections = []
    for body in root.findall("body"):
        prefix = "примечания" if body.get("name") in ("notes", "comments") else ""
        _fb2_sections(body, sections, prefix)
    return title, sections


def read_fb2(path):
    with open(path, "rb") as f:
        return parse_fb2(f.read())


def read_fb2_zip(path):
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
        if not names:
            raise ValueError("в архиве нет .fb2")
        return parse_fb2(z.read(names[0]))


READERS = {".txt": read_txt, ".doc": read_doc, ".docx": read_docx, ".pdf": read_pdf,
           ".epub": read_epub, ".fb2": read_fb2, ".fb2.zip": read_fb2_zip}


def file_ext(path):
    low = path.lower()
    if low.endswith(".fb2.zip"):
        return ".fb2.zip"
    return os.path.splitext(low)[1]


PLACEHOLDER_TITLES = {"unknown", "unknown title", "untitled", "без названия", "документ", "document"}


def read_book(path):
    """(название, [(метка, текст)]) для поддерживаемого файла."""
    reader = READERS.get(file_ext(path))
    if reader is None:
        raise ValueError(f"формат не поддерживается: {path}")
    title, sections = reader(path)
    if title and title.strip().lower() in PLACEHOLDER_TITLES:
        title = None
    return title, [(label or "", normalize_text(text)) for label, text in sections if text.strip()]


def normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("­", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ============ Фрагменты ============
SENTENCE_RE = re.compile(r"(?<=[.!?…»])\s+(?=[«\"(A-ZА-ЯЁ0-9—-])")


def _pieces(text, size):
    """Абзацы, а слишком длинные абзацы — предложения (и, если надо, куски по size)."""
    for para in re.split(r"\n\s*\n|\n(?=\s)", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            yield para
            continue
        for sent in SENTENCE_RE.split(para):
            while len(sent) > size:
                cut = sent.rfind(" ", 0, size)
                cut = cut if cut > size // 2 else size
                yield sent[:cut].strip()
                sent = sent[cut:]
            if sent.strip():
                yield sent.strip()


def chunk_text(text, size=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """Фрагменты около size символов по границам абзацев и предложений, с перекрытием overlap."""
    chunks, cur = [], []
    for piece in _pieces(text, size):
        if cur and sum(len(p) + 1 for p in cur) + len(piece) > size:
            chunks.append("\n".join(cur))
            tail, n = [], 0
            for p in reversed(cur):                 # перекрытие — последние предложения
                if n + len(p) > overlap:
                    break
                tail.insert(0, p)
                n += len(p) + 1
            if not tail and overlap:
                last = cur[-1]
                cut = last.find(" ", max(0, len(last) - overlap))
                tail = [last[cut + 1:]] if cut >= 0 else []
            cur = tail
        cur.append(piece)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ============ Индекс и поиск ============
class Hit:
    def __init__(self, chunk_id, path, title, label, text, score, coverage=0.0, sim=None):
        self.chunk_id, self.path, self.title, self.label, self.text = chunk_id, path, title, label, text
        self.score, self.coverage, self.sim = score, coverage, sim

    def source(self):
        return f"«{self.title}»" + (f", {self.label}" if self.label else "")

    def __repr__(self):
        return f"Hit({self.source()}, score={self.score:.3f}, coverage={self.coverage:.2f})"


class SentenceTransformerEmbedder:
    """Векторы multilingual-e5 (бесплатно, локально, понимает русский). Нужен
    pip install sentence-transformers; модель скачивается при первом запуске (~470 МБ для small)."""

    def __init__(self, model_name="intfloat/multilingual-e5-small"):
        from sentence_transformers import SentenceTransformer
        self.name = model_name
        self.model = SentenceTransformer(model_name)

    def __call__(self, texts, is_query=False):
        prefix = "query: " if is_query else "passage: "
        vecs = self.model.encode([prefix + t for t in texts], normalize_embeddings=True,
                                 batch_size=32, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


def _pack(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", [x / norm for x in vec]).tobytes()


def _unpack(blob):
    a = array.array("f")
    a.frombytes(blob)
    return a


class KnowledgeBase:
    def __init__(self, folder, db_path, lemmatizer=None, embedder=None, top_k=TOP_K,
                 min_coverage=MIN_COVERAGE, chunk_chars=CHUNK_CHARS, overlap=CHUNK_OVERLAP,
                 dense_min_sim=DENSE_MIN_SIM):
        self.folder = os.path.abspath(folder)
        self.db_path = db_path
        self.lemmatizer = lemmatizer or Lemmatizer()
        self.embedder = embedder
        self.top_k, self.min_coverage, self.dense_min_sim = top_k, min_coverage, dense_min_sim
        self.chunk_chars, self.overlap = chunk_chars, overlap
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, mtime REAL, size INTEGER, title TEXT,
                chunks INTEGER, error TEXT, indexed_at REAL);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL, ord INTEGER, label TEXT,
                text TEXT, nterms INTEGER);
            CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
            CREATE TABLE IF NOT EXISTS postings (term TEXT NOT NULL, chunk_id INTEGER NOT NULL, tf INTEGER);
            CREATE INDEX IF NOT EXISTS idx_postings_term ON postings(term);
            CREATE INDEX IF NOT EXISTS idx_postings_chunk ON postings(chunk_id);
            CREATE TABLE IF NOT EXISTS vectors (chunk_id INTEGER PRIMARY KEY, vec BLOB);
        """)
        signature = json.dumps([INDEX_VERSION, self.lemmatizer.name, chunk_chars, overlap,
                                getattr(embedder, "name", None) if embedder else None])
        if self._meta("signature") != signature:
            if self._meta("signature"):
                log.info("RAG: настройки индекса изменились — индекс будет построен заново")
            for table in ("files", "chunks", "postings", "vectors"):
                self.db.execute(f"DELETE FROM {table}")
            self._set_meta("signature", signature)
            self.db.commit()

    def _meta(self, key):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self):
        self.db.close()

    # ---------- индексация ----------
    def scan(self):
        """{относительный путь: (mtime, size)} поддерживаемых файлов папки (с подпапками)."""
        found = {}
        if not os.path.isdir(self.folder):
            return found
        for root, dirs, files in os.walk(self.folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith((".", "~$")) or file_ext(name) not in SUPPORTED_EXT:
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, self.folder).replace(os.sep, "/")
                st = os.stat(full)
                found[rel] = (st.st_mtime, st.st_size)
        return found

    def sync(self):
        """Индексирует новые и изменённые файлы, удаляет из индекса исчезнувшие.
        Возвращает (сколько проиндексировано, сколько удалено)."""
        on_disk = self.scan()
        known = {p: (m, s) for p, m, s in self.db.execute("SELECT path, mtime, size FROM files")}
        removed = [p for p in known if p not in on_disk]
        for p in removed:
            self._remove(p)
            log.info("RAG: %s удалён из индекса", p)
        changed = [p for p, ms in on_disk.items() if known.get(p) != ms]
        for p in sorted(changed):
            self.index_file(p, stat=on_disk[p])
        if removed:
            self.db.commit()
        return len(changed), len(removed)

    def _remove(self, rel):
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE path=?", (rel,))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            self.db.execute(f"DELETE FROM postings WHERE chunk_id IN ({marks})", part)
            self.db.execute(f"DELETE FROM vectors WHERE chunk_id IN ({marks})", part)
        self.db.execute("DELETE FROM chunks WHERE path=?", (rel,))
        self.db.execute("DELETE FROM files WHERE path=?", (rel,))

    def index_file(self, rel, stat=None):
        """(Пере)индексирует один файл папки. Возвращает число фрагментов (0 при ошибке)."""
        full = os.path.join(self.folder, rel)
        t0 = time.time()
        if stat is None:
            st = os.stat(full)
            stat = (st.st_mtime, st.st_size)
        self._remove(rel)
        title, error, n = None, None, 0
        try:
            title, sections = read_book(full)
            for label, text in sections:
                for chunk in chunk_text(text, self.chunk_chars, self.overlap):
                    self._add_chunk(rel, n, label, chunk)
                    n += 1
            if not n:
                error = "текста не найдено"
        except Exception as e:
            error = str(e) or e.__class__.__name__
            log.warning("RAG: не удалось прочитать %s: %s", rel, error)
        title = title or self.default_title(rel)
        self.db.execute("INSERT INTO files(path,mtime,size,title,chunks,error,indexed_at) "
                        "VALUES(?,?,?,?,?,?,?)", (rel, stat[0], stat[1], title, n, error, time.time()))
        if self.embedder and n:
            self._embed(rel)
        self.db.commit()
        log.info("RAG: %s — %d фрагментов за %.1f с%s", rel, n, time.time() - t0,
                 f" (ошибка: {error})" if error else "")
        return n

    @staticmethod
    def default_title(rel):
        name = posixpath.basename(rel)
        for ext in SUPPORTED_EXT:
            if name.lower().endswith(ext):
                return name[:-len(ext)]
        return name

    def _add_chunk(self, rel, order, label, text):
        words = terms(text, self.lemmatizer)
        cur = self.db.execute("INSERT INTO chunks(path,ord,label,text,nterms) VALUES(?,?,?,?,?)",
                              (rel, order, label, text, len(words)))
        tf = {}
        for w in words:
            tf[w] = tf.get(w, 0) + 1
        self.db.executemany("INSERT INTO postings(term,chunk_id,tf) VALUES(?,?,?)",
                            [(w, cur.lastrowid, c) for w, c in tf.items()])

    def _embed(self, rel):
        rows = self.db.execute("SELECT id, label, text FROM chunks WHERE path=? ORDER BY ord", (rel,)).fetchall()
        title = self.db.execute("SELECT title FROM files WHERE path=?", (rel,)).fetchone()
        title = title[0] if title else rel
        for i in range(0, len(rows), 64):
            batch = rows[i:i + 64]
            vecs = self.embedder([f"{title}. {label}\n{text}" for _, label, text in batch])
            self.db.executemany("INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES(?,?)",
                                [(cid, _pack(v)) for (cid, _, _), v in zip(batch, vecs)])

    # ---------- поиск ----------
    def search(self, query, k=None):
        """Фрагменты, подходящие к запросу (лучшие первыми). Пустой список — в базе ничего нет."""
        k = k or self.top_k
        q_terms = list(dict.fromkeys(terms(query, self.lemmatizer)))
        lexical = self._bm25(q_terms, k * 4) if q_terms else []
        dense = self._dense(query, k * 4) if self.embedder else []
        if not dense:
            ranked = lexical[:k]
        else:
            # объединение рангов (Reciprocal Rank Fusion)
            scores = {}
            for lst in (lexical, dense):
                for rank, (cid, _s, _c, _sim) in enumerate(lst):
                    scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + rank)
            info = {}
            for cid, s, c, sim in lexical + dense:
                prev = info.get(cid, (cid, 0.0, 0.0, None))
                info[cid] = (cid, s or prev[1], max(c, prev[2]), sim if sim is not None else prev[3])
            ranked = sorted((info[cid] for cid in scores), key=lambda x: -scores[x[0]])[:k]
            ranked = [(cid, scores[cid], c, sim) for cid, _s, c, sim in ranked]
        return [self._hit(*item) for item in ranked]

    def _bm25(self, q_terms, limit):
        n_chunks, total = self.db.execute("SELECT COUNT(*), COALESCE(SUM(nterms),0) FROM chunks").fetchone()
        if not n_chunks:
            return []
        avg_len = (total / n_chunks) or 1.0
        lengths, scores, matched = {}, {}, {}
        for term in q_terms:
            rows = self.db.execute(
                "SELECT p.chunk_id, p.tf, c.nterms FROM postings p JOIN chunks c ON c.id=p.chunk_id "
                "WHERE p.term=?", (term,)).fetchall()
            if not rows:
                continue
            idf = math.log(1 + (n_chunks - len(rows) + 0.5) / (len(rows) + 0.5))
            for cid, tf, length in rows:
                lengths[cid] = length
                denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len)
                scores[cid] = scores.get(cid, 0.0) + idf * tf * (BM25_K1 + 1) / denom
                matched[cid] = matched.get(cid, 0) + 1
        need = max(1, math.ceil(len(q_terms) * self.min_coverage - 1e-9))
        found = [(cid, s, matched[cid] / len(q_terms), None) for cid, s in scores.items()
                 if matched[cid] >= need]
        found.sort(key=lambda x: (-x[2], -x[1]))
        return found[:limit]

    def _dense(self, query, limit):
        rows = self.db.execute("SELECT chunk_id, vec FROM vectors").fetchall()
        if not rows:
            return []
        q = _unpack(_pack(self.embedder([query], is_query=True)[0]))
        try:
            import numpy as np
            mat = np.frombuffer(b"".join(v for _, v in rows), dtype=np.float32).reshape(len(rows), -1)
            sims = mat @ np.asarray(q, dtype=np.float32)
            pairs = [(rows[i][0], float(sims[i])) for i in range(len(rows))]
        except ImportError:
            pairs = [(cid, sum(a * b for a, b in zip(_unpack(v), q))) for cid, v in rows]
        pairs = [p for p in pairs if p[1] >= self.dense_min_sim]
        pairs.sort(key=lambda p: -p[1])
        return [(cid, sim, 0.0, sim) for cid, sim in pairs[:limit]]

    def _hit(self, cid, score, coverage, sim):
        path, label, text = self.db.execute("SELECT path, label, text FROM chunks WHERE id=?", (cid,)).fetchone()
        title = self.db.execute("SELECT title FROM files WHERE path=?", (path,)).fetchone()
        return Hit(cid, path, title[0] if title else path, label, text, score, coverage, sim)

    # ---------- сведения ----------
    def books(self):
        """[(путь, название, фрагментов, ошибка)] проиндексированных файлов."""
        return self.db.execute("SELECT path, title, chunks, error FROM files ORDER BY path").fetchall()

    def stats(self):
        books = self.books()
        return {"files": len(books), "ok": sum(1 for b in books if b[2]),
                "chunks": sum(b[2] or 0 for b in books),
                "errors": [(b[0], b[3]) for b in books if b[3]],
                "lemmatizer": self.lemmatizer.name,
                "embeddings": getattr(self.embedder, "name", None) if self.embedder else None}


# ============ Промпт и разбор ответа ============
SYSTEM_PROMPT = (
    "Ты отвечаешь на вопрос строго по фрагментам из базы знаний (книг), которые даны ниже.\n"
    "Правила:\n"
    "1. Используй только сведения из фрагментов. Не добавляй ничего от себя, не используй "
    "собственные знания, не додумывай и не обобщай сверх написанного.\n"
    f"2. Если во фрагментах нет ответа на вопрос, ответь ровно одним словом: {NO_ANSWER}\n"
    "3. После каждого утверждения ставь в квадратных скобках номер фрагмента, на котором оно "
    "основано, например [2].\n"
    "4. Пиши по-русски, ясно и по существу. Можно цитировать фрагменты."
)


CHAT_RULES = (
    "Ниже даны фрагменты из базы знаний (книг), найденные по сообщению собеседника. "
    "Отвечай только по ним:\n"
    "1. Бери сведения только из фрагментов. Ничего не добавляй от себя, не используй собственные "
    "знания, не додумывай и не обобщай сверх написанного. История чата — только чтобы понять, "
    "о чём спрашивают; сведения из неё не бери.\n"
    f"2. Если во фрагментах нет ответа, ответь ровно одним словом: {NO_ANSWER}\n"
    "3. После каждого утверждения ставь в квадратных скобках номер фрагмента, например [2].\n"
    "4. Сохраняй свою роль и манеру речи, но не жертвуй точностью."
)


def format_fragments(hits, max_chars=MAX_CONTEXT_CHARS):
    """«[1] источник\nтекст\n\n[2] …» — сколько фрагментов влезает в max_chars (первый — всегда).
    Возвращает (текст, сколько фрагментов вошло)."""
    blocks, used = [], 0
    for i, h in enumerate(hits, 1):
        block = f"[{i}] {h.source()}\n{h.text}"
        if blocks and used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks), len(blocks)


def build_prompt(query, hits, max_chars=MAX_CONTEXT_CHARS):
    """(system, user) — строгий промпт: ответ только по пронумерованным фрагментам."""
    fragments, _n = format_fragments(hits, max_chars)
    user = "ФРАГМЕНТЫ:\n\n" + fragments + f"\n\nВОПРОС: {query.strip()}"
    return SYSTEM_PROMPT, user


def is_no_answer(raw):
    return re.sub(r"^[\s*_«\"'`.:—-]+", "", raw or "").upper().startswith(NO_ANSWER)


def parse_answer(raw, hits):
    """Текст ответа с источниками или None, если модель сказала, что ответа во фрагментах нет."""
    raw = (raw or "").strip()
    if not raw or is_no_answer(raw):
        return None
    text = re.sub(rf"\s*{NO_ANSWER}\s*", " ", raw).strip()
    cited = []
    for group in re.findall(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]", text):
        for n in re.split(r"\s*[,;]\s*", group):
            n = int(n)
            if 1 <= n <= len(hits) and n not in cited:
                cited.append(n)
    if cited:
        lines = [f"[{n}] {hits[n - 1].source()}" for n in sorted(cited)]
    else:
        lines = list(dict.fromkeys(h.source() for h in hits))
    return f"{text}\n\nИсточники:\n" + "\n".join(lines)


def main(argv=None):
    """Проверка из командной строки: python rag.py ПАПКА "запрос" — индексирует и ищет."""
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not argv:
        print("Использование: python rag.py ПАПКА [запрос]")
        return 2
    folder = argv[0]
    kb = KnowledgeBase(folder, os.path.join(folder, "..", "rag_index.db"))
    kb.sync()
    print(json.dumps(kb.stats(), ensure_ascii=False, indent=1))
    if len(argv) > 1:
        for h in kb.search(" ".join(argv[1:])):
            print(f"\n{h!r}\n{h.text[:500]}")
    kb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
