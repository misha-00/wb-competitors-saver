# wb_competitors_app.py
# Быстрая публичная версия:
# 1) "Сгенерировать пакет" — параллельно качает фото (ThreadPool),
#    делает Excel с картинками, коллаж и сразу готовит ZIP.
# 2) "Скачать архив" — отдаёт готовый ZIP.

import re
import io
import json
import time
import math
import zipfile
import pathlib
import concurrent.futures as cf
import requests
import streamlit as st
import pandas as pd
from PIL import Image
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Настройки производительности ----------------
MAX_WORKERS = 24               # общее число параллельных загрузок (без фанатизма)
PER_PRODUCT_WORKERS = 8        # ограничение внутри одного товара
REQ_TIMEOUT = (5, 12)          # (connect, read) таймауты по умолчанию
RETRY_TOTAL = 2                # число авто-повторов на 5xx/429
DEFAULT_SLIDES = 10            # если WB не сообщает pics
THUMB = (360, 360)             # превью для коллажа
CELL_PX = (160, 160)           # размер картинки в Excel-ячейке

# ---------------- UI ----------------
st.set_page_config(page_title="WB Competitors Saver (FAST)", page_icon="⚡", layout="wide")
st.title("⚡ WB Competitors Saver — ускоренная версия")

st.caption(
    "Вставь ссылки WB (по одной в строке) → нажми **«Сгенерировать пакет»**.\n"
    "Сервис параллельно скачает фото по артикулам, соберёт **Excel с картинками**, **коллаж** и **ZIP**."
)

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def get_session_root(name_hint: str | None = None) -> pathlib.Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root_name = name_hint.strip() if name_hint and name_hint.strip() else f"WB_Save_{ts}"
    root = pathlib.Path.cwd() / root_name
    root.mkdir(parents=True, exist_ok=True)
    return root

def parse_input_urls(text: str) -> list[str]:
    return [u.strip() for u in (text or "").splitlines() if u.strip()]

# ---------------- HTTP Session с keep-alive и retry ----------------
def make_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",    # без gzip — меньше CPU, быстрее I/O на маленьких файлах
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"])
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

# ---------------- WB utils ----------------
def extract_nm_id(url: str) -> str | None:
    try:
        u = urlparse(url)
        q = parse_qs(u.query)
        if "nm" in q and q["nm"]:
            return re.sub(r"\D", "", q["nm"][0])
        m = re.search(r"/catalog/(\d+)", u.path)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

def fetch_card_json(session: requests.Session, nm: str) -> dict | None:
    api = (f"https://card.wb.ru/cards/v2/detail"
           f"?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}")
    r = session.get(api, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    prods = data.get("data", {}).get("products", [])
    return prods[0] if prods else None

def parse_basics(prod: dict) -> tuple[str | None, str | None, int]:
    if not prod:
        return None, None, 0
    title = prod.get("name")
    brand = prod.get("brand")
    pics = int(prod.get("pics") or 0)
    if pics == 0:
        photos = (prod.get("media") or {}).get("photos") or []
        pics = len(photos)
    return title, brand, pics

def candidate_image_urls(nm_id: int, idx: int) -> list[str]:
    vol = nm_id // 100000
    part = nm_id // 1000
    # webp обычно быстрее и меньше — пробуем первым
    exts = (".webp", ".jpg")
    baskets = [f"https://basket-{i:02d}.wb.ru" for i in range(1, 33)]
    baskets += [f"https://basket-{i:02d}.wbbasket.ru" for i in range(1, 33)]
    urls = []
    for host in baskets:
        base = f"{host}/vol{vol}/part{part}/{nm_id}/images/big/{idx}"
        for ext in exts:
            urls.append(base + ext)
    return urls

# ---------------- Параллельные загрузки ----------------
def download_one_image(session: requests.Session, urls: list[str], dest_path: pathlib.Path) -> bool:
    """Пробуем варианты URL до первого успеха. Если файл уже есть — пропускаем."""
    if dest_path.with_suffix(".webp").exists() or dest_path.with_suffix(".jpg").exists():
        return True
    for u in urls:
        try:
            r = session.get(u, timeout=REQ_TIMEOUT, stream=False)
            if r.status_code == 200 and int(r.headers.get("Content-Length", "1")) > 0:
                ext = ".webp" if u.endswith(".webp") else ".jpg"
                with open(dest_path.with_suffix(ext), "wb") as f:
                    f.write(r.content)
                return True
        except Exception:
            pass
    return False

def download_product_images(session: requests.Session, nm: int, pics: int, subdir: pathlib.Path) -> int:
    ensure_dir(subdir)
    saved = 0
    # задачи по слайдам (1..pics)
    tasks = list(range(1, pics + 1))
    # ограничим конкуренцию внутри товара
    workers = min(PER_PRODUCT_WORKERS, max(1, math.ceil(pics / 2)))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for i in tasks:
            urls = candidate_image_urls(nm, i)
            dest_stub = subdir / f"{i}"
            futures.append(pool.submit(download_one_image, session, urls, dest_stub))
        for fut in cf.as_completed(futures):
            try:
                if fut.result():
                    saved += 1
            except Exception:
                pass
    return saved

# ---------------- Подсчёт слайдов ----------------
def detect_max_slides(root: pathlib.Path) -> int:
    max_slides = 0
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        imgs = list(sub.glob("*.jpg")) + list(sub.glob("*.webp"))
        if not imgs:
            continue
        local_max = 0
        for p in imgs:
            try:
                local_max = max(local_max, int(p.stem))
            except Exception:
                pass
        if local_max == 0:
            local_max = len(imgs)
        max_slides = max(max_slides, local_max)
    return max_slides or 1

# ---------------- Excel + изображения ----------------
def _image_to_png_bytes(path: pathlib.Path, max_w: int, max_h: int) -> BytesIO | None:
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((max_w, max_h))
        bio = BytesIO()
        im.save(bio, format="PNG", optimize=True)
        bio.seek(0)
        return bio
    except Exception:
        return None

def save_excel_with_images(root: pathlib.Path,
                           summary_rows: list[dict],
                           limit_slides: int = 10,
                           cell_w_px: int = 160,
                           cell_h_px: int = 160) -> pathlib.Path:
    out = root / "listing_matrix.xlsx"
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df_sum = pd.DataFrame(summary_rows)
        if not df_sum.empty:
            cols = ["order", "nm_id", "brand", "title", "slides", "folder"]
            df_sum = df_sum[[c for c in cols if c in df_sum.columns]]
        df_sum.to_excel(writer, sheet_name="Сводка", index=False)

        wb = writer.book
        ws = wb.add_worksheet("Матрица")

        competitors = sorted([p for p in root.iterdir() if p.is_dir()])
        nm_ids = [c.name.split("_")[-1] for c in competitors]

        header_fmt = wb.add_format({"bold": True, "align": "center"})
        ws.write(0, 0, "")
        for col, nm in enumerate(nm_ids, start=1):
            ws.write(0, col, nm, header_fmt)

        row_labels_fmt = wb.add_format({"align": "center"})
        for r in range(1, limit_slides + 1):
            ws.write(r, 0, f"{r} слайд", row_labels_fmt)

        col_width_chars = max(12, int(cell_w_px / 7))
        row_height_pts = max(24, int(cell_h_px / 1.33))
        ws.set_column(0, 0, 12)
        for c in range(1, len(nm_ids) + 1):
            ws.set_column(c, c, col_width_chars)
        for r in range(1, limit_slides + 1):
            ws.set_row(r, row_height_pts)

        x_offset = 5
        y_offset = 5

        for col, comp_dir in enumerate(competitors, start=1):
            imgs = sorted(list(comp_dir.glob("*.jpg")) + list(comp_dir.glob("*.webp")),
                          key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
            for r_idx in range(limit_slides):
                if r_idx < len(imgs):
                    bio = _image_to_png_bytes(imgs[r_idx], cell_w_px, cell_h_px)
                    if bio:
                        ws.insert_image(r_idx + 1, col, imgs[r_idx].name,
                                        {"image_data": bio, "x_offset": x_offset, "y_offset": y_offset})
    return out

# ---------------- Коллаж ----------------
def save_collage(root: pathlib.Path, limit_slides: int = 10) -> pathlib.Path | None:
    competitors = sorted([p for p in root.iterdir() if p.is_dir()])
    if not competitors:
        return None
    grid = []
    max_rows = 0
    for c in competitors:
        imgs = sorted(list(c.glob("*.jpg")) + list(c.glob("*.webp")),
                      key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
        imgs = imgs[:limit_slides]
        max_rows = max(max_rows, len(imgs))
        grid.append(imgs)
    if max_rows == 0:
        return None
    cols = len(grid)
    rows = max_rows
    cell_w, cell_h = THUMB
    pad = 10
    W = cols * cell_w + (cols + 1) * pad
    H = rows * cell_h + (rows + 1) * pad
    canvas = Image.new("RGB", (W, H), (245, 245, 245))
    for x, col_imgs in enumerate(grid):
        for y in range(rows):
            if y < len(col_imgs):
                try:
                    img = Image.open(col_imgs[y]).convert("RGB")
                    img.thumbnail(THUMB)
                    ox = pad + x * (cell_w + pad) + (cell_w - img.width)//2
                    oy = pad + y * (cell_h + pad) + (cell_h - img.height)//2
                    canvas.paste(img, (ox, oy))
                except Exception:
                    pass
    out = root / "matrix_preview.jpg"
    canvas.save(out, format="JPEG", quality=85)
    return out

# ---------------- ZIP ----------------
def make_zip_bytes(root: pathlib.Path) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in root.rglob("*"):
            if path.is_file():
                z.write(path, arcname=str(path.relative_to(root)))
    mem.seek(0)
    return mem.read()

# ---------------- Интерфейс ----------------
with st.form("form_links"):
    urls_text = st.text_area("Ссылки на товары WB (по одной на строке)", height=160)
    session_name = st.text_input("Имя общей папки (необязательно)", placeholder="Анализ_товаров")
    c1, c2 = st.columns(2)
    with c1:
        do_generate = st.form_submit_button("🚀 Сгенерировать пакет (быстро)")
    with c2:
        do_download_zip = st.form_submit_button("⬇️ Скачать архив")

# состояние
if "root_path" not in st.session_state:
    st.session_state["root_path"] = ""
if "zip_bytes" not in st.session_state:
    st.session_state["zip_bytes"] = None
if "zip_name" not in st.session_state:
    st.session_state["zip_name"] = None
if "last_excel" not in st.session_state:
    st.session_state["last_excel"] = None
if "last_collage" not in st.session_state:
    st.session_state["last_collage"] = None

# --- Генерация всего ---
if do_generate:
    links = parse_input_urls(urls_text)
    if not links:
        st.error("Добавь хотя бы одну ссылку."); st.stop()

    root = get_session_root(session_name)
    st.session_state["root_path"] = str(root)

    status = st.empty()
    progress = st.progress(0)

    session = make_http_session()

    ok_list, err_list = [], []
    total = len(links)

    # Пул на все товары (но картинки внутри товара качаются своим мини-пулом)
    with cf.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total or 1)) as outer_pool:
        futures = []
        for idx, url in enumerate(links, start=1):
            futures.append((idx, url, outer_pool.submit(
                lambda _idx=idx, _url=url: ( _idx, _url, None, None, 0, None )
            )))  # заглушка, реальная работа ниже (последовательно, чтобы статус красиво шёл)

        # Последовательно обрабатываем товары, но внутри — параллельные картинки
        for idx, url in [(i, u) for i, u, _ in futures]:
            status.write(f"Товар {idx}/{total}: {url}")
            nm_raw = extract_nm_id(url)
            if not nm_raw:
                err_list.append((url, "Не найден артикул (nm_id)"))
                progress.progress(idx/total); continue

            nm = int(nm_raw)
            try:
                prod = fetch_card_json(session, nm_raw)
            except Exception as e:
                err_list.append((url, f"API ошибка: {e}"))
                progress.progress(idx/total); continue

            title, brand, pics = parse_basics(prod)
            if pics <= 0:
                pics = DEFAULT_SLIDES

            subdir = root / f"{idx:03d}_{nm}"
            ensure_dir(subdir)

            meta = {"url": url, "nm_id": nm, "title": title, "brand": brand, "saved_at": datetime.now().isoformat()}
            (subdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            saved = download_product_images(session, nm, pics, subdir)

            if saved > 0:
                ok_list.append((url, subdir.name, saved))
            else:
                err_list.append((url, "Не удалось сохранить изображения"))

            progress.progress(idx/total)

    # Сводка/таблица/коллаж
    competitors = sorted([p for p in root.iterdir() if p.is_dir()])
    summary_rows = []
    for sub in competitors:
        nm = sub.name.split("_")[-1]
        imgs = sorted(list(sub.glob("*.jpg")) + list(sub.glob("*.webp")),
                      key=lambda p: (int(p.stem) if p.stem.isdigit() else 9999))
        meta = sub / "meta.json"
        title = brand = None
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                title, brand = m.get("title"), m.get("brand")
            except Exception:
                pass
        summary_rows.append({
            "order": sub.name.split("_")[0],
            "nm_id": nm,
            "brand": brand,
            "title": title,
            "slides": len(imgs),
            "folder": sub.name
        })

    max_slides = detect_max_slides(root)
    xlsx_path = save_excel_with_images(root, summary_rows, limit_slides=max_slides,
                                       cell_w_px=CELL_PX[0], cell_h_px=CELL_PX[1])
    collage_path = save_collage(root, min(max_slides, 10))

    # ZIP
    zip_bytes = make_zip_bytes(root)
    st.session_state["zip_bytes"] = zip_bytes
    st.session_state["zip_name"] = f"{root.name}.zip"
    st.session_state["last_excel"] = xlsx_path
    st.session_state["last_collage"] = collage_path

    st.success("Готово! Пакет сформирован.")
    st.write(f"📁 Папка: {root}")
    st.write(f"📊 Excel: {xlsx_path.name}")
    if collage_path:
        st.write(f"🖼 Коллаж: {collage_path.name}")

    # Кнопки прямых скачиваний
    with open(xlsx_path, "rb") as f:
        st.download_button("⬇️ Скачать только Excel", data=f.read(),
                           file_name=xlsx_path.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if collage_path and collage_path.exists():
        with open(collage_path, "rb") as f:
            st.download_button("⬇️ Скачать только коллаж (JPG)", data=f.read(),
                               file_name=collage_path.name, mime="image/jpeg")
    st.download_button("⬇️ Скачать архив (всё вместе)",
                       data=zip_bytes, file_name=st.session_state["zip_name"], mime="application/zip")

    if ok_list:
        st.subheader("✅ Сохранены")
        for url, folder, cnt in ok_list:
            st.write(f"- {folder} — {cnt} фото — {url}")
    if err_list:
        st.subheader("⚠️ Ошибки")
        for url, msg in err_list:
            st.write(f"- {url}: {msg}")

# --- Скачать ZIP без пересборки ---
if do_download_zip:
    if not st.session_state["zip_bytes"]:
        st.error("Архив ещё не готов. Сначала нажми «Сгенерировать пакет».")
    else:
        st.download_button("⬇️ Скачать архив (всё вместе)",
                           data=st.session_state["zip_bytes"],
                           file_name=st.session_state["zip_name"],
                           mime="application/zip")
